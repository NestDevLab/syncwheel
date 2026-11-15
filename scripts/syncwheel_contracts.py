"""Shared command and publication policy vocabulary.

Stable values used by both CLI options and their domain validators.
"""

import datetime

from syncwheel_manifest import AUTHORITY_CLASS_SOURCE_CHANGE, AUTHORITY_CLASS_RUNTIME_CHANGE

COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND = 'tree-equivalent-state-cas'

COORDINATION_REPAIR_FAST_FORWARD_BACKEND = 'fast-forward-state-cas'

COORDINATION_REPAIR_MISSING_REF_BACKEND = 'missing-ref-create-cas'

COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND = 'state-digest-migration'

COORDINATION_REPAIR_DIGEST_HEAL_BACKEND = 'state-digest-heal'

AUTHORITY_GRANTABLE_CLASSES = (AUTHORITY_CLASS_SOURCE_CHANGE, AUTHORITY_CLASS_RUNTIME_CHANGE)

UPDATE_MODES = {'off', 'notify', 'auto'}

RECONCILE_MODES = {'standard', 'resume'}

GITHUB_PR_MERGE_METHODS = {'squash', 'merge', 'rebase'}

GITHUB_PR_MERGE_CHECKS = {'all', 'none'}
GITHUB_PR_MERGE_INTEGRATION = {'required', 'advisory'}


PROFILE_FILENAME = 'profile.local.json'

ZERO_OBJECT_ID = '0' * 40


def iso_utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def parse_coordination_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def syncwheel_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


MANAGED_REF_MOVE_AUTH_ENV = 'SYNCWHEEL_REF_MOVE_AUTH'


INTEGRATION_STRATEGIES = {'cherry-pick', 'merge-stacks'}
