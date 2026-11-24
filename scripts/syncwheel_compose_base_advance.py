"""Fail-closed proof for fast-forward publication across an advanced base."""

import os
from pathlib import Path
import re
import subprocess

from syncwheel_errors import SyncwheelError


_OID = re.compile(r'^[0-9a-f]{40}$')


def verify_compose_base_advance(repo, previous, selected, old_tip, new_tip, target_tip):
    """Prove a published tip is retained by a merge of the exact target base.

    This proves ancestry and immutable input identities, not that an advancing
    target branch remains unchanged after the final observation. Source and
    coordination-state leases remain the publisher's separate responsibility.
    """
    old_base = previous.get('base')
    new_base = selected.get('base')
    ids = (old_base, new_base, old_tip, new_tip, target_tip)
    if any(not isinstance(value, str) or not _OID.fullmatch(value) for value in ids):
        raise SyncwheelError('compose base advance requires exact SHA-1 inputs')
    old_adoption = previous.get('adopted_history')
    old_commits = previous.get('commits')
    if (not isinstance(old_commits, list) or not old_commits
            or any(not isinstance(commit, str) or not _OID.fullmatch(commit) for commit in old_commits)
            or len(old_commits) != len(set(old_commits)) or old_commits[-1] != old_tip
            or old_adoption not in (None, {'base': old_base, 'tip': old_tip})
            or (old_adoption is not None and old_commits != [old_tip])):
        raise SyncwheelError('compose base advance requires the exact published source recipe')
    if selected.get('commits') != [new_tip] or selected.get('adopted_history') != {
        'base': new_base, 'tip': new_tip,
    }:
        raise SyncwheelError('compose base advance requires an exact adopted source tip')
    if new_base != target_tip or new_base == old_base:
        raise SyncwheelError('compose base advance requires the observed advanced target tip')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1',
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0')
    repo = Path(repo).resolve(strict=True)

    def git(*args):
        try:
            return subprocess.run(['git', '-C', str(repo), *args], env=env,
                                  text=True, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncwheelError('compose base advance cannot inspect Git history') from exc

    shallow = git('rev-parse', '--is-shallow-repository')
    if shallow.returncode or shallow.stdout.strip() != 'false':
        raise SyncwheelError('compose base advance refuses shallow history')
    graft = git('rev-parse', '--git-path', 'info/grafts')
    if graft.returncode:
        raise SyncwheelError('compose base advance cannot inspect grafts')
    graft_path = Path(graft.stdout.strip())
    if not graft_path.is_absolute():
        graft_path = repo / graft_path
    if graft_path.exists() and graft_path.stat().st_size:
        raise SyncwheelError('compose base advance refuses grafted history')
    replacements = git('for-each-ref', '--format=%(refname)', 'refs/replace/')
    if replacements.returncode or replacements.stdout.strip():
        raise SyncwheelError('compose base advance refuses replacement refs')
    if old_adoption is None:
        parent = old_base
        for commit in old_commits:
            item = git('rev-list', '--parents', '-n', '1', commit)
            if item.returncode or item.stdout.split() != [commit, parent]:
                raise SyncwheelError('compose base advance requires a complete linear published recipe')
            parent = commit
    for old, new in ((old_base, new_base), (old_tip, new_tip), (new_base, new_tip)):
        if git('merge-base', '--is-ancestor', old, new).returncode:
            raise SyncwheelError('compose base advance requires preserved source and base ancestry')
    parents = git('rev-list', '--parents', '-n', '1', new_tip)
    fields = parents.stdout.split()
    if parents.returncode or len(fields) != 3 or fields[0] != new_tip or fields[2] != new_base:
        raise SyncwheelError('compose base advance requires a two-parent merge of the exact target tip')
    feature_tip = fields[1]
    if git('merge-base', '--is-ancestor', old_tip, feature_tip).returncode:
        raise SyncwheelError('compose base advance feature parent must retain the published tip')
    chain = git('rev-list', '--first-parent', '--parents', f'{old_tip}..{feature_tip}')
    if chain.returncode:
        raise SyncwheelError('compose base advance cannot inspect feature-side history')
    prior_base = old_base
    prior_merges = []
    for line in reversed(chain.stdout.splitlines()):
        fields = line.split()
        if len(fields) == 2:
            continue
        if len(fields) != 3:
            raise SyncwheelError('compose base advance refuses non-linear feature-side history')
        prior_target = fields[2]
        if (git('merge-base', '--is-ancestor', prior_base, prior_target).returncode
                or git('merge-base', '--is-ancestor', prior_target, new_base).returncode):
            raise SyncwheelError('compose base advance refuses an unrelated or reordered prior base merge')
        prior_merges.append({'commit': fields[0], 'base': prior_target})
        prior_base = prior_target
    return {'oldBase': old_base, 'newBase': new_base, 'oldTip': old_tip,
            'newTip': new_tip, 'featureTip': feature_tip, 'observedTargetTip': target_tip,
            'priorBaseMerges': prior_merges}
