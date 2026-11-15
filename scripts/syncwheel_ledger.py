"""Pure projection of append-only lifecycle events into current task state.

File locking, tail recovery and durable persistence are caller responsibilities.
"""

from syncwheel_manifest import manifest_digest, stack_integration_commits, stack_integration_only_commits

LEDGER_SCHEMA_VERSION = 1

COORDINATION_PUBLICATION_TERMINAL_EVENT_TYPES = (
    'coordination_publish_completed',
    'coordination_publish_abandoned',
)


def default_ledger_state():
    return {
        'schema_version': LEDGER_SCHEMA_VERSION,
        'last_seq': 0,
        'event_count': 0,
        'manifest': None,
        'integration': {},
        'stacks': {},
        'channels': {},
        'coordination_publications': {},
        'recent_events': [],
    }


def branch_ref_matches(candidate, branch):
    return candidate == branch or candidate.endswith(f'/{branch}')


def apply_ledger_event(state, event):
    payload = event.get('payload') or {}
    state['last_seq'] = event['seq']
    state['event_count'] += 1
    state['recent_events'].append(event)
    state['recent_events'] = state['recent_events'][-20:]

    if event['type'] in ('manifest_initialized', 'manifest_saved'):
        active_ids = set()
        for summary in payload.get('stacks') or []:
            stack = state['stacks'].setdefault(summary['id'], {'id': summary['id']})
            stack.update(summary)
            stack['active_in_manifest'] = True
            stack['last_manifest_seq'] = event['seq']
            active_ids.add(summary['id'])
        for stack_id, stack in state['stacks'].items():
            if stack_id not in active_ids:
                stack['active_in_manifest'] = False
        active_channel_ids = set()
        channels = state.setdefault('channels', {})
        for summary in payload.get('channels') or []:
            channel = channels.setdefault(summary['id'], {'id': summary['id']})
            channel.update(summary)
            channel['active_in_manifest'] = True
            channel['last_manifest_seq'] = event['seq']
            active_channel_ids.add(summary['id'])
        for channel_id, channel in channels.items():
            if channel_id not in active_channel_ids:
                channel['active_in_manifest'] = False
        integration = payload.get('integration') or {}
        state['manifest'] = {
            'manifest_path': payload.get('manifest_path'),
            'manifest_hash': payload.get('manifest_hash'),
            'reason': payload.get('reason'),
            'integration': integration,
            'active_stacks': sorted(active_ids),
            'active_channels': sorted(active_channel_ids),
            'last_seq': event['seq'],
        }
        state['integration'].update({
            'branch': integration.get('branch'),
            'base': integration.get('base'),
            'strategy': integration.get('strategy'),
            'active_stacks': list(integration.get('stacks') or []),
            'last_manifest_seq': event['seq'],
        })
        return state

    if event['type'] in (
        'channel_operation_started', 'channel_operation_prepared',
        'channel_operation_receipt',
    ):
        channel_id = payload.get('channel')
        if channel_id:
            channels = state.setdefault('channels', {})
            channel = channels.setdefault(channel_id, {'id': channel_id})
            channel['last_operation_id'] = payload.get('operationId')
            channel['last_operation'] = payload.get('operation')
            channel['last_operation_seq'] = event['seq']
            channel['last_operation_status'] = (
                payload.get('status')
                if event['type'] == 'channel_operation_receipt' else 'pending'
            )
        return state

    if event['type'] in ('channel_applied', 'channel_published', 'channel_closed'):
        channel_id = payload.get('channel')
        if channel_id:
            channels = state.setdefault('channels', {})
            channel = channels.setdefault(channel_id, {'id': channel_id})
            channel.update({
                'branch': payload.get('branch') or channel.get('branch'),
                'last_event_type': event['type'],
                'last_event_seq': event['seq'],
                'last_plan_digest': payload.get('planDigest'),
                'last_tip': payload.get('tip') or channel.get('last_tip'),
                'last_remote_tip': payload.get('publishedRevision') or channel.get('last_remote_tip'),
            })
            if event['type'] == 'channel_closed':
                channel['active_in_manifest'] = False
        return state

    if event['type'] == 'stack_rebuilt':
        stack = state['stacks'].setdefault(payload['stack'], {'id': payload['stack']})
        stack.update({
            'branch': payload.get('branch') or stack.get('branch'),
            'base': payload.get('base') or stack.get('base'),
            'integration_branch': payload.get('integration_branch') or stack.get('integration_branch'),
            'last_rebuilt_tip': payload.get('after_tip'),
            'last_rebuild_seq': event['seq'],
            'active_in_manifest': stack.get('active_in_manifest', False),
        })
        return state

    if event['type'] == 'stack_pushed':
        stack = state['stacks'].setdefault(payload['stack'], {'id': payload['stack']})
        stack.update({
            'branch': payload.get('branch') or stack.get('branch'),
            'last_pushed_tip': payload.get('tip'),
            'last_push_remote': payload.get('remote'),
            'last_push_seq': event['seq'],
        })
        return state

    if event['type'] in ('integration_rebuilt', 'integration_aligned_remote', 'integration_pushed'):
        state['integration'].update({
            'branch': payload.get('branch') or state['integration'].get('branch'),
            'last_tip': payload.get('after_tip') or payload.get('tip') or state['integration'].get('last_tip'),
            'last_remote_ref': payload.get('remote_ref') or state['integration'].get('last_remote_ref'),
            'last_push_remote': payload.get('remote') or state['integration'].get('last_push_remote'),
            'last_event_type': event['type'],
            'last_event_seq': event['seq'],
        })
        return state

    if event['type'] == 'coordination_publish_intent':
        token = payload.get('operation_token')
        if token:
            record = state.setdefault('coordination_publications', {}).setdefault(
                token, {'operation_token': token}
            )
            record.update({
                'scope': payload.get('scope'),
                'intent_seq': event['seq'],
                'owner': payload.get('owner'),
            })
        return state

    if event['type'] in COORDINATION_PUBLICATION_TERMINAL_EVENT_TYPES:
        token = payload.get('operation_token')
        if token:
            record = state.setdefault('coordination_publications', {}).setdefault(
                token, {'operation_token': token}
            )
            if record.get('terminal'):
                record.setdefault('duplicate_terminals', []).append(event['type'])
                return state
            record.update({
                'terminal': event['type'],
                'terminal_seq': event['seq'],
                'terminal_status': (
                    payload.get('status') or payload.get('coordination_status')
                ),
            })
        return state

    if event['type'] == 'stack_closed':
        stack_id = payload.get('stack')
        if stack_id:
            stack = state['stacks'].setdefault(stack_id, {'id': stack_id})
            stack.update({
                'branch': payload.get('branch') or stack.get('branch'),
                'active_in_manifest': False,
                'closed_reason': payload.get('reason', 'merged'),
                'last_closed_seq': event['seq'],
            })
        return state

    return state


def reduce_ledger_state(events):
    state = default_ledger_state()
    for event in events:
        apply_ledger_event(state, event)
    return state


def manifest_channel_history_summary(channel):
    summary = {
        'id': channel['id'],
        'branch': channel['branch'],
        'base': channel['base'],
        'baseRevision': channel['baseRevision'],
        'remote': channel['remote'],
        'lifecycle': channel['lifecycle'],
        'composition': [
            {
                'stack': entry['stack'],
                'branch': entry['branch'],
                'stackBase': entry['stackBase'],
                'stackBaseRevision': entry['stackBaseRevision'],
                'branchRevision': entry['branchRevision'],
                'commits': list(entry['commits']),
                'dependsOn': list(entry.get('dependsOn', [])),
            }
            for entry in channel.get('composition', [])
        ],
    }
    if channel.get('expiry'):
        summary['expiry'] = dict(channel['expiry'])
    if channel.get('resolution'):
        summary['resolution'] = dict(channel['resolution'])
    return summary


def manifest_stack_history_summary(stack):
    summary = {
        'id': stack['id'],
        'branch': stack['branch'],
        'base': stack['base'],
        'target_remote': stack['target_remote'],
        'target_branch': stack['target_branch'],
        'integration_branch': stack.get('integration_branch'),
        'state': stack.get('state', 'published'),
        'commits': list(stack['commits']),
        'integration_commits': stack_integration_commits(stack),
        'integration_only_commits': stack_integration_only_commits(stack),
        'meta': dict(stack.get('meta', {})),
    }
    if stack.get('depends_on'):
        summary['depends_on'] = list(stack['depends_on'])
    return summary


def manifest_event_payload(manifest_path, manifest, reason, context=None):
    return {
        'manifest_path': str(manifest_path),
        'manifest_hash': manifest_digest(manifest),
        'reason': reason,
        'context': context or {},
        'integration': {
            'branch': manifest['integration']['branch'],
            'base': manifest['integration']['base'],
            'strategy': manifest['integration'].get('strategy', 'cherry-pick'),
            'stacks': list(manifest['integration'].get('stacks', [])),
        },
        'stacks': [manifest_stack_history_summary(stack) for stack in manifest['stacks']],
        'channels': [
            manifest_channel_history_summary(channel)
            for channel in manifest.get('channels', [])
        ],
    }
