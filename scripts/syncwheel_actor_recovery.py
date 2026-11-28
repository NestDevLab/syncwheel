"""Repository delivery recovery, using independent maintainer attestations.

This is an operational gate, not an identity/security boundary against arbitrary
Git clients. Enable only with repository-approved, server-protected authority and
state refs. The candidate must not control either ref. Authority is a separate
JSON document: schema_version=1, repository=<stable id>, records={<id>: record}.
Records are immutable by ID. Each includes kind and repository. incident records
include incident, actor, delivery, proven=True, attributed=True and evidence;
recovery records include incident, recovered=True, attribution_verified=True and
evidence; publication records include actor, delivery, head and operation;
review records include operation, head, actor, reviewer, outcome='approved' and
evidence; outcome records include operation, head, actor, delivery,
outcome='published' or 'not_published', and evidence. Authoritative actor IDs
identify the responsible agent across lanes, never the reporter or lane owner.

The maintainer attests publication identity independently of the invoking shell.
Do not derive these records from USER, local receipt claims or device identity.
Source verification/recovery remains the authority's responsibility. Refs and
repository identity are configured by the repository owner, never the candidate.
Provision the initial state explicitly with empty_state(repository). No missing
store, malformed record, unknown outcome or stale lease implies permission.

prepare reserves one publication per actor until verified terminal reconciliation.
Call it immediately before the real publisher, preserving its operation ID. A
crash leaves the reservation; reconcile a proven outcome, never blindly republish.
complete consumes repeat-review debt only after proven publication, not preview,
tests, failed push, merge preparation or reporting. New incidents remain owed.
The durable effect-start CAS serializes confirmed holds and the beginning of
the effect interval. Evidence arriving after it remains pending until terminal
readback. This does not pretend that Git and forge share an atomic transaction.
"""

import copy
import contextlib
import contextvars
import json
import os
import re
import subprocess

from syncwheel_errors import SyncwheelError

PATH = '.syncwheel/actor-recovery.json'
STATE_REF = 'refs/heads/syncwheel/actor-recovery'
_TRANSPORT_PREFIX = contextvars.ContextVar('actor_recovery_git_prefix', default=())
_TRANSPORT_ENV = contextvars.ContextVar('actor_recovery_git_environment', default=None)


def empty_state(repository):
    return {'schema_version': 1, 'repository': repository, 'incidents': {},
            'operations': {}, 'reviewed_through': {}, 'pending_incidents': {}}


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise SyncwheelError('actor recovery requires ' + name)
    return value


def _head(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
        raise SyncwheelError('actor recovery requires exact candidate SHA')
    return value


def _record(authority, reference, kind, repository):
    _text(reference, 'independent authority reference')
    if authority.get('schema_version') != 1 or authority.get('repository') != repository:
        raise SyncwheelError('actor recovery authority identity mismatch')
    records = authority.get('records')
    if not isinstance(records, dict):
        raise SyncwheelError('invalid independent authority records')
    raw = records.get(reference)
    if not isinstance(raw, dict) or raw.get('kind') != kind or raw.get('repository') != repository:
        raise SyncwheelError('missing independent authority attestation: ' + reference)
    return copy.deepcopy(raw)


def transition(state, authority, action, reference, *, review=None):
    """Pure intent reducer; only authority record references enter this interface."""
    result = copy.deepcopy(state)
    repository = _text(result.get('repository'), 'repository')
    if result.get('schema_version') != 1:
        raise SyncwheelError('unsupported actor recovery state schema')
    kinds = {'confirm': 'incident', 'recover': 'recovery', 'prepare': 'publication', 'start': 'publication',
             'complete': 'outcome'}
    if action not in kinds:
        raise SyncwheelError('unsupported actor recovery action')
    record = _record(authority, reference, kinds[action], repository)
    incidents, operations = result['incidents'], result['operations']
    if action == 'confirm':
        if record.get('proven') is not True or record.get('attributed') is not True:
            raise SyncwheelError('warning or discovery is not a proven attributed violation')
        for name in ('incident', 'actor', 'delivery', 'evidence'):
            _text(record.get(name), name)
        incident = record['incident']
        pending = result.setdefault('pending_incidents', {})
        existing = incidents.get(incident) or pending.get(incident)
        if existing is not None and existing['proof'] != record:
            raise SyncwheelError('incident identity collision; retain original attribution')
        if any(item.get('phase') == 'executing' and item['outcome'] is None
               and item['publication']['delivery'] == record['delivery'] for item in operations.values()):
            if incident not in incidents:
                pending.setdefault(incident, {'proof': record, 'recovery': None})
        else:
            incidents.setdefault(incident, {'proof': record, 'recovery': None})
    elif action == 'recover':
        incident = incidents.get(record.get('incident'))
        if incident is None or record.get('recovered') is not True or record.get('attribution_verified') is not True:
            raise SyncwheelError('recovery and attribution must be independently verified')
        _text(record.get('evidence'), 'recovery evidence')
        if incident['recovery'] is not None and incident['recovery'] != record:
            raise SyncwheelError('recovery identity collision')
        incident['recovery'] = record
    elif action == 'prepare':
        for name in ('operation', 'actor', 'delivery'):
            _text(record.get(name), name)
        _head(record.get('head'))
        operation, actor = record['operation'], record['actor']
        existing = operations.get(operation)
        if existing is not None:
            if existing['publication'] != record:
                raise SyncwheelError('publication identity collision')
            # A resumed preparation never authorizes repeating an uncertain effect.
            raise SyncwheelError('publication already reserved; reconcile its actual outcome')
        blockers = delivery_blockers(result, record['delivery'])
        if blockers:
            raise SyncwheelError('affected delivery awaits verified recovery: ' + ', '.join(blockers))
        if any(item['publication']['actor'] == actor and item['outcome'] is None
               for item in operations.values()):
            raise SyncwheelError('agent has an uncertain publication; reconcile first')
        confirmed = sorted(key for key, item in incidents.items() if item['proof']['actor'] == actor)
        covered = result['reviewed_through'].get(actor, [])
        owed = len(confirmed) >= 2 and bool(set(confirmed) - set(covered))
        accepted = None
        if owed:
            accepted = _record(authority, review or '', 'review', repository)
            if (accepted.get('operation') != operation or accepted.get('head') != record['head']
                    or accepted.get('actor') != actor or accepted.get('outcome') != 'approved'
                    or _text(accepted.get('reviewer'), 'reviewer') == actor):
                raise SyncwheelError('independent review must match agent, operation and exact SHA')
            _text(accepted.get('evidence'), 'review evidence')
        operations[operation] = {'publication': record, 'review': accepted, 'phase': 'prepared',
                                 'covered_incidents': confirmed if owed else [], 'outcome': None}
    elif action == 'start':
        operation = operations.get(record.get('operation'))
        if operation is None or operation['publication'] != record or operation['outcome'] is not None:
            raise SyncwheelError('effect requires its exact pending publication reservation')
        if operation.get('phase') != 'prepared':
            raise SyncwheelError('effect already started; reconcile instead of repeating publication')
        if delivery_blockers(result, record['delivery']):
            raise SyncwheelError('confirmed delivery hold won before publication effect')
        if any(other is not operation and other.get('phase') == 'executing'
               and other['outcome'] is None and other['publication']['delivery'] == record['delivery']
               for other in operations.values()):
            raise SyncwheelError('same delivery already has an executing publication')
        # A second incident elsewhere by this actor may have arrived after prepare.
        confirmed = {key for key, item in incidents.items() if item['proof']['actor'] == record['actor']}
        owed = len(confirmed) >= 2 and bool(confirmed - set(result['reviewed_through'].get(record['actor'], [])))
        if owed and (operation['review'] is None or confirmed - set(operation['covered_incidents'])):
            raise SyncwheelError('new confirmed actor incident requires a fresh reviewed reservation')
        operation['phase'] = 'executing'
    else:
        operation = operations.get(record.get('operation'))
        if operation is None:
            raise SyncwheelError('publication has no durable reservation')
        publication = operation['publication']
        if any(record.get(key) != publication[key] for key in ('operation', 'actor', 'delivery', 'head')):
            raise SyncwheelError('publication outcome identity mismatch')
        if record.get('outcome') not in ('published', 'not_published'):
            raise SyncwheelError('uncertain publication remains reserved')
        _text(record.get('evidence'), 'publication outcome evidence')
        if operation['outcome'] is not None and operation['outcome'] != record:
            raise SyncwheelError('publication terminal outcome collision')
        operation['outcome'] = record
        for key, pending in list(result.setdefault('pending_incidents', {}).items()):
            if pending['proof']['delivery'] == publication['delivery']:
                incidents.setdefault(key, pending)
                del result['pending_incidents'][key]
        if record['outcome'] == 'published' and operation['review'] is not None:
            actor = publication['actor']
            result['reviewed_through'][actor] = sorted(set(
                result['reviewed_through'].get(actor, []) + operation['covered_incidents']))
    return result


def delivery_blockers(state, delivery):
    return sorted(key for key, item in state['incidents'].items()
                  if item['proof']['delivery'] == delivery and item['recovery'] is None)


def run_command(store, action, reference=None, *, review=None, apply=False):
    """CLI facade: preview first; caller emits JSON and owns approved config.

    `prepare` is a reservation only: an existing operation always requires
    outcome reconciliation, never a second remote attempt. No permission from
    local policy preferences or a successful preview is implied.
    """
    snapshot = store.snapshot()
    if action == 'status':
        return {'applied': False, **snapshot}
    proposed = transition(snapshot['state'], snapshot['authority'], action, reference, review=review)
    result = {'applied': False, 'state_sha': snapshot['state_sha'],
              'authority_sha': snapshot['authority_sha'], 'proposed': proposed}
    if action == 'confirm':
        incident = _record(snapshot['authority'], reference, 'incident', store.repository)['incident']
        result['confirmation'] = ('pending_effect' if incident in proposed.get('pending_incidents', {})
                                  else 'confirmed')
    if apply:
        result['state_sha'] = store.apply(snapshot, action, reference, review=review)
        result['applied'] = True
    return result


def from_profile(root, profile):
    configuration = profile.get('actor_recovery')
    if configuration is None:
        return None
    if not isinstance(configuration, dict) or configuration.get('enabled') is not True:
        raise SyncwheelError('actor_recovery requires explicit enabled=true or removal to disable')
    for name in ('remote', 'state_ref', 'authority_ref', 'repository'):
        _text(configuration.get(name), name)
    targets = configuration.get('product_refs')
    if not isinstance(targets, list) or not targets or any(
            not isinstance(ref, str) or not ref.startswith('refs/heads/')
            for ref in targets):
        raise SyncwheelError('actor_recovery requires explicit product refs')
    if configuration['state_ref'] in targets or configuration['authority_ref'] in targets:
        raise SyncwheelError('actor recovery state and authority refs cannot be product targets')
    return GitRecoveryStore(root, configuration['remote'], configuration['state_ref'],
                            configuration['authority_ref'], configuration['repository'])


def repository_profile(root, remote='origin'):
    """Discover owner opt-in on the actual publication remote, never candidate files.

    The fixed shared state ref also owns the complete policy. Its CAS is the
    common serialization point for policy changes, holds and effect start.
    """
    reader = GitRecoveryStore(root, remote, STATE_REF, 'refs/heads/discovery-placeholder', 'discovery')
    destinations = push_destinations(root, remote)
    if len(destinations) != 1:
        raise SyncwheelError('repository recovery requires one canonical push destination')
    transport = destinations[0]
    lines = reader.git('ls-remote', '--refs', transport, STATE_REF).splitlines()
    if not lines:
        return {}
    if len(lines) != 1 or lines[0].split()[1] != STATE_REF:
        raise SyncwheelError('ambiguous canonical actor recovery authority')
    sha = _head(lines[0].split()[0])
    reader.git('fetch', '--quiet', '--no-write-fetch-head', transport, sha)
    try:
        document = json.loads(reader.git('show', sha + ':' + PATH))
        configuration = copy.deepcopy(document['policy'])
    except (ValueError, KeyError, TypeError) as exc:
        raise SyncwheelError('canonical actor recovery policy is missing or invalid') from exc
    if not isinstance(configuration, dict):
        raise SyncwheelError('canonical actor recovery policy must be an object')
    if configuration.get('state_ref') != STATE_REF or configuration.get('repository') != document.get('repository'):
        raise SyncwheelError('canonical actor recovery policy identity mismatch')
    # Freeze the transport endpoint; changing a local alias cannot redirect this store.
    configuration['_declared_remote'] = configuration.get('remote')
    configuration['remote'] = transport
    result = {'actor_recovery': configuration}
    from_profile(root, result)  # Strict policy validation, without initializing anything.
    return result


def _live_policy(root, plan):
    return repository_profile(root, plan.get('recoveryRemote') or plan.get('targetRemote') or 'origin')


def _scoped_policy(profile, plan):
    policy = profile.get('actor_recovery')
    if policy is None or 'refs/heads/' + plan['baseBranch'] not in policy['product_refs']:
        return None
    return policy


def push_destinations(root, remote):
    """All effective push URLs, including pushInsteadOf rewrites, before effects."""
    reader = GitRecoveryStore(root, remote, STATE_REF, 'refs/heads/discovery-placeholder', 'discovery')
    try:
        destinations = reader.git('remote', 'get-url', '--push', '--all', remote).splitlines()
    except SyncwheelError:
        destinations = _explicit_push_destination(reader, remote)
    if not destinations or any(not value.strip() for value in destinations):
        raise SyncwheelError('publication push destination is unavailable')
    # Metadata reads of a resolved URL must observe that same endpoint. A
    # second rewrite would otherwise inspect a different repository than the
    # original push. Refuse such configurations rather than guess or transport.
    for destination in destinations:
        if _explicit_push_destination(reader, destination) != [destination]:
            raise SyncwheelError('push destination is not stable for canonical observation; use an explicit stable endpoint')
    return list(dict.fromkeys(destinations))


def _explicit_push_destination(reader, value):
    # --get-url does not contact the endpoint. It applies Git's insteadOf
    # rules, but has no push mode. Never guess pushInsteadOf routing: require
    # a named remote so get-url --push can prove that transport selection.
    rules = reader.git('config', '--null', '--get-regexp', r'^url\..*\.pushinsteadof$',
                       allow_missing=True)
    for entry in rules.split('\0'):
        if not entry:
            continue
        key, separator, prefix = entry.partition('\n')
        if not separator:
            raise SyncwheelError('cannot verify configured push URL rules')
        if value.startswith(prefix):
            raise SyncwheelError('explicit URL affected by pushInsteadOf requires a named remote before publication')
    return reader.git('ls-remote', '--get-url', value).splitlines()


def merge_binding(store, profile, plan, reference):
    """Attested responsible agent, never inferred from forge login/lane owner."""
    snapshot = store.snapshot()
    canonical = copy.deepcopy(snapshot['state'].get('policy'))
    if not isinstance(canonical, dict):
        raise SyncwheelError('shared repository publication policy is missing')
    canonical['_declared_remote'] = canonical.get('remote')
    canonical['remote'] = store.remote
    if canonical != profile['actor_recovery']:
        raise SyncwheelError('complete shared publication policy changed during observation')
    attestation = _record(snapshot['authority'], reference, 'publication', store.repository)
    target = 'refs/heads/' + plan['baseBranch']
    _text(attestation.get('delivery'), 'affected work delivery identity')
    if (target not in profile['actor_recovery']['product_refs'] or attestation.get('target_ref') != target
            or attestation.get('operation') != plan['operationId']
            or attestation.get('head') != plan['headSha']
            or attestation.get('pull_request') != plan['pullRequest']['number']
            or attestation.get('base_sha') != plan['pullRequest']['baseSha']
            or attestation.get('base_branch') != plan['baseBranch']
            or plan['repository'] != store.repository):
        raise SyncwheelError('publication attestation does not bind this exact PR, target and candidate')
    return snapshot, attestation


def reconcile_reference(root, profile, reference, observe):
    """Native recovery even after a crash before the ordinary PR ledger started."""
    remote = profile.get('_remote') or (profile.get('actor_recovery') or {}).get('remote') or 'origin'
    profile = repository_profile(root, remote)
    store = from_profile(root, profile)
    if store is None:
        raise SyncwheelError('actor recovery is not enabled')
    snapshot = store.snapshot()
    binding = _record(snapshot['authority'], reference, 'publication', store.repository)
    operation = snapshot['state']['operations'].get(binding.get('operation'))
    if operation is None or operation['publication'] != binding:
        raise SyncwheelError('publication has no matching durable reservation')
    plan = {'repository': store.repository, 'baseBranch': binding['base_branch'],
            'targetRemote': store.remote, 'recoveryRemote': store.remote,
            'operationId': binding['operation'], 'headSha': binding['head'],
            'pullRequest': {'number': binding['pull_request'], 'baseSha': binding['base_sha']}}
    observation = observe({'operation': 'observe', 'repository': store.repository,
                           'baseBranch': binding['base_branch'],
                           'pullRequestNumber': binding['pull_request']})
    return reconcile_merge(root, profile, plan, reference=reference, observation=observation)


def reserve_merge(root, profile, plan, *, reference, review=None):
    profile = _live_policy(root, plan)
    expected = (plan.get('actorRecovery') or {}).get('policy')
    scoped = _scoped_policy(profile, plan)
    if expected != scoped:
        raise SyncwheelError('complete repository publication policy changed after preview')
    if scoped is None:
        return None
    store = from_profile(root, profile)
    if store is None:
        return None
    snapshot, attestation = merge_binding(store, profile, plan, reference)
    sha = store.apply(snapshot, 'prepare', reference, review=review)
    return {'state_sha': sha, 'operation': attestation['operation']}


def start_merge(root, plan, reference):
    profile = _live_policy(root, plan)
    scoped = _scoped_policy(profile, plan)
    if (plan.get('actorRecovery') or {}).get('policy') != scoped:
        raise SyncwheelError('repository publication policy changed before effect start')
    if scoped is None:
        return None
    store = from_profile(root, profile)
    snapshot, _ = merge_binding(store, profile, plan, reference)
    return store.apply(snapshot, 'start', reference)


def preview_merge(root, profile, plan, *, reference, review=None):
    policy = None
    try:
        profile = _live_policy(root, plan)
        policy = profile.get('actor_recovery')
        if policy is None or 'refs/heads/' + plan['baseBranch'] not in policy.get('product_refs', {}):
            return None
        store = from_profile(root, profile)
        snapshot, binding = merge_binding(store, profile, plan, reference)
        transition(snapshot['state'], snapshot['authority'], 'prepare', reference, review=review)
        return {'status': 'ready', 'state_sha': snapshot['state_sha'],
                'authority_sha': snapshot['authority_sha'], 'actor': binding['actor'],
                'policy': profile['actor_recovery']}
    except SyncwheelError as exc:
        return {'status': 'blocked', 'detail': str(exc), 'policy': policy}


def reconcile_merge(root, profile, plan, *, reference, observation):
    """Consume only exact remote observation from the existing forge adapter.

    This function is internal to the merge owner, never a CLI JSON-import API.
    Failed/unknown execution return codes are not proof of publication failure.
    Unknown observations retain the reservation for later readback.
    """
    profile = _live_policy(root, plan)
    if (profile.get('actor_recovery') is None or 'refs/heads/' + plan['baseBranch']
            not in profile['actor_recovery'].get('product_refs', {})):
        return None
    store = from_profile(root, profile)
    if store is None:
        return None
    snapshot, binding = merge_binding(store, profile, plan, reference)
    pr = observation.get('pr') if isinstance(observation, dict) else None
    if (not isinstance(pr, dict) or observation.get('repository') != plan['repository']
            or pr.get('number') != plan['pullRequest']['number']
            or pr.get('headRefOid') != plan['headSha']
            or pr.get('baseRefName') != plan['baseBranch']):
        return {'status': 'unknown', 'operation': binding['operation']}
    if pr.get('state') == 'MERGED' and pr.get('mergeCommit') and pr.get('mergedAt'):
        outcome = 'published'
    elif (pr.get('state') == 'OPEN'
          and pr.get('baseRefOid') == plan['pullRequest']['baseSha']):
        outcome = 'not_published'
    else:
        return {'status': 'unknown', 'operation': binding['operation']}
    record_value = {key: binding[key] for key in ('operation', 'head', 'actor', 'delivery')}
    record_value.update(kind='outcome', repository=store.repository, outcome=outcome,
                        evidence=json.dumps({'source': 'forge-observation', 'repository': plan['repository'],
                                  'pr': pr['number'], 'head': pr['headRefOid'],
                                  'merge_commit': pr.get('mergeCommit'), 'merged_at': pr.get('mergedAt')}, sort_keys=True))
    # The observer supplies this trusted internal record; public callers can only
    # reference independent authority records. Reuse the same CAS reducer.
    return {'status': outcome,
            'state_sha': store.apply_observed_outcome(snapshot, record_value)}


def guard_product_push(root, profile, command):
    """Ref synchronization stays free; the opted-in product target uses PR gate.

    All in-process Git pushes pass here via the common runner. Out-of-process
    Git/forge clients remain the server's responsibility. Dry runs have no effect.
    """
    if command[:2] != ['git', 'push'] or '--dry-run' in command or '-n' in command:
        return
    tokens = command[2:]
    # Syncwheel publication owners emit explicit remote/refspecs. Unknown forms
    # cannot establish that a governed product target is outside their effect.
    positional, selected_remote = [], None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == '--':
            positional.extend(tokens[index + 1:])
            break
        if token in ('--repo', '--receive-pack', '--exec', '-o', '--push-option'):
            if index + 1 >= len(tokens):
                raise SyncwheelError('incomplete push scope')
            if token == '--repo':
                selected_remote = tokens[index + 1]
            index += 2
            continue
        if token.startswith('--repo='):
            selected_remote = token.split('=', 1)[1]
        elif not token.startswith('-'):
            positional.append(token)
        index += 1
    if selected_remote is None and positional:
        selected_remote = positional.pop(0)
    remote = selected_remote or 'origin'
    # Git fans out one named push to every pushurl. Inspect all of them before
    # the runner starts Git, so a later protected destination cannot be reached
    # after an earlier unprotected destination has already been changed.
    for destination in push_destinations(root, remote):
        profile = repository_profile(root, destination)
        config = profile.get('actor_recovery')
        if config is None:
            continue
        ambiguous = not positional or any(option in tokens for option in ('--all', '--mirror', '--prune'))
        for spec in positional:
            target = spec.lstrip('+').split(':')[-1]
            if '*' in target:
                ambiguous = True
            if not target.startswith('refs/'):
                target = 'refs/heads/' + target
            if target in (STATE_REF, config['authority_ref']):
                raise SyncwheelError('repository recovery authority requires its dedicated owner path')
            if target in config['product_refs']:
                raise SyncwheelError('product target requires attributed PR publication via stack merge-pr')
        if ambiguous:
            raise SyncwheelError('cannot prove push excludes opted-in product targets; use explicit work refs')


def _push_position(command):
    if not command or command[0] != 'git':
        return None
    index = 1
    while index < len(command) and command[index].startswith('-'):
        if command[index] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'):
            index += 2
        elif (command[index] in ('--no-pager', '--paginate', '-p', '--bare', '--no-replace-objects',
                                 '--literal-pathspecs', '--glob-pathspecs', '--noglob-pathspecs',
                                 '--icase-pathspecs', '--no-optional-locks', '--no-lazy-fetch', '--no-advice')
              or command[index].startswith(('--git-dir=', '--work-tree=', '--namespace=', '--config-env='))):
            index += 1
        else:
            if 'push' in command[index:]:
                raise SyncwheelError('cannot verify Git push transport with these global options')
            return None
    return index if command[index:index + 1] == ['push'] else None


def push_command(command):
    """Recognize a native push without discarding its transport selectors."""
    index = _push_position(command)
    return ['git', *command[index:]] if index is not None else None


@contextlib.contextmanager
def transport_context(command, environment):
    index = _push_position(command)
    prefix_token = _TRANSPORT_PREFIX.set(tuple(command[1:index]) if index is not None else ())
    env_token = _TRANSPORT_ENV.set(environment)
    try:
        yield
    finally:
        _TRANSPORT_PREFIX.reset(prefix_token)
        _TRANSPORT_ENV.reset(env_token)


class GitRecoveryStore:
    """Explicit repository-owned shared refs with exact remote compare-and-swap.

    Both refs must already exist and be protected by server policy. No activation,
    initialization, remote choice or actor inference occurs implicitly. The
    authority is read independently of the candidate and pinned by commit ID.
    A CAS conflict never retries or merges evidence automatically.
    """

    def __init__(self, root, remote, state_ref, authority_ref, repository):
        self.root, self.remote, self.repository = root, remote, repository
        self.state_ref, self.authority_ref = state_ref, authority_ref
        if state_ref == authority_ref or not all(
                re.fullmatch(r'refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*', ref)
                and '..' not in ref and not ref.endswith('/')
                for ref in (state_ref, authority_ref)):
            raise SyncwheelError('distinct repository-approved shared refs required')
        if not remote or remote.startswith('-'):
            raise SyncwheelError('repository-approved remote required')

    def git(self, *args, input=None, allow_missing=False):
        environment = _TRANSPORT_ENV.get()
        if environment is None:
            environment = os.environ.copy()
            for name in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                         'GIT_PREFIX', 'GIT_NAMESPACE'):
                environment.pop(name, None)
        process = subprocess.run(['git', '-C', str(self.root), *_TRANSPORT_PREFIX.get(), *args], input=input,
                                 text=True, capture_output=True, check=False, env=environment)
        if process.returncode == 1 and allow_missing:
            return ''
        if process.returncode:
            raise SyncwheelError('actor recovery Git operation refused: ' + process.stderr.strip())
        return process.stdout.strip()

    def read_ref(self, ref):
        lines = self.git('ls-remote', '--refs', self.remote, ref).splitlines()
        if len(lines) != 1 or lines[0].split()[1] != ref:
            raise SyncwheelError('authoritative actor recovery ref unavailable')
        sha = _head(lines[0].split()[0])
        self.git('fetch', '--quiet', '--no-write-fetch-head', self.remote, sha)
        if self.git('ls-tree', '-r', '--name-only', sha).splitlines() != [PATH]:
            raise SyncwheelError('actor recovery requires a dedicated single-document ref; preserve other content')
        try:
            document = json.loads(self.git('show', sha + ':' + PATH))
        except (ValueError, TypeError) as exc:
            raise SyncwheelError('invalid actor recovery authority/state document') from exc
        if (not isinstance(document, dict) or document.get('schema_version') != 1
                or document.get('repository') != self.repository):
            raise SyncwheelError('actor recovery repository identity mismatch')
        return sha, document

    def snapshot(self):
        state_sha, state = self.read_ref(self.state_ref)
        if state.get('policy') is not None and (
                state['policy'].get('state_ref') != self.state_ref
                or state['policy'].get('authority_ref') != self.authority_ref):
            raise SyncwheelError('shared repository recovery policy changed; reload owner authority')
        authority_sha, authority = self.read_ref(self.authority_ref)
        return {'state_sha': state_sha, 'authority_sha': authority_sha,
                'state': state, 'authority': authority}

    def apply(self, snapshot, action, reference, *, review=None):
        current = self.snapshot()
        if current != snapshot:
            raise SyncwheelError('actor recovery evidence changed; re-read and review')
        proposed = transition(current['state'], current['authority'], action, reference, review=review)
        return self._save(current, proposed)

    def apply_observed_outcome(self, snapshot, observation):
        current = self.snapshot()
        if current != snapshot:
            raise SyncwheelError('actor recovery changed during remote readback; reconcile again')
        authority = copy.deepcopy(current['authority'])
        authority['records']['internal-forge-observation'] = observation
        proposed = transition(current['state'], authority, 'complete', 'internal-forge-observation')
        return self._save(current, proposed)

    def _save(self, current, proposed):
        if proposed == current['state']:
            return current['state_sha']
        destinations = push_destinations(self.root, self.remote)
        if len(destinations) != 1:
            raise SyncwheelError('recovery state CAS requires one canonical push destination')
        # No worktree, index, local branch or application ref is changed.
        blob = self.git('hash-object', '-w', '--stdin', input=json.dumps(proposed, sort_keys=True) + '\n')
        subtree = self.git('mktree', input='100644 blob ' + blob + '\tactor-recovery.json\n')
        tree = self.git('mktree', input='040000 tree ' + subtree + '\t.syncwheel\n')
        commit = self.git('commit-tree', tree, '-p', current['state_sha'],
                          input='Record verified delivery recovery\n')
        # This lease protects a fast-forward child of the observed state. It
        # never rewrites ancestry; stale writers fail instead of forcing.
        self.git('push', '--porcelain', '--force-with-lease=' + self.state_ref + ':' + current['state_sha'],
                 destinations[0], commit + ':' + self.state_ref)
        return commit
