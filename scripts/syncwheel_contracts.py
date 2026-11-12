"""Shared command and publication policy vocabulary.

Stable values used by both CLI options and their domain validators.
"""

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


PROFILE_FILENAME = 'profile.local.json'

ZERO_OBJECT_ID = '0' * 40
