"""Evidence for a narrowly scoped adopted-to-linear-recipe transition."""

import os
from pathlib import Path
import re
import subprocess

from syncwheel_errors import SyncwheelError


def verify_adoption_transition(repo, previous, selected, source_tip, published_tip):
    """Prove that an adopted linear history is retained exactly, in order.

    The caller still owns ref/claim checks and the atomic publication lease.
    """
    adoption = previous.get('adopted_history')
    if not isinstance(adoption, dict) or selected.get('adopted_history'):
        raise SyncwheelError('adoption transition requires an adopted source and ordinary recipe')
    base, old_tip = adoption.get('base'), adoption.get('tip')
    commits = selected.get('commits')
    if (not all(isinstance(x, str) and re.fullmatch(r'[0-9a-f]{40}', x)
                for x in (base, old_tip, source_tip, published_tip))
            or not isinstance(commits, list) or not commits
            or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{40}', x) for x in commits)
            or len(commits) != len(set(commits))):
        raise SyncwheelError('adoption transition requires exact unique commit IDs')
    if published_tip != old_tip:
        raise SyncwheelError('adoption transition published source differs from adopted tip')
    if selected.get('base') != previous.get('base') or selected.get('base') != base:
        raise SyncwheelError('adoption transition must preserve the exact adopted base')
    if commits[-1] != source_tip or old_tip not in commits:
        raise SyncwheelError('adoption transition must retain the adopted tip in its exact source sequence')
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    root = Path(repo).resolve(strict=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], text=True,
                              capture_output=True, env=env)
    identity = git('rev-parse', '--show-toplevel')
    if identity.returncode or Path(identity.stdout.strip()).resolve() != root:
        raise SyncwheelError('adoption transition repository identity mismatch')
    shallow = git('rev-parse', '--is-shallow-repository')
    if shallow.returncode or shallow.stdout.strip() != 'false':
        raise SyncwheelError('adoption transition refuses shallow history')
    graft = git('rev-parse', '--git-path', 'info/grafts')
    if graft.returncode:
        raise SyncwheelError('adoption transition cannot inspect graft configuration')
    graft_path = Path(graft.stdout.strip())
    if not graft_path.is_absolute():
        graft_path = root / graft_path
    if graft_path.exists() and graft_path.stat().st_size:
        raise SyncwheelError('adoption transition refuses grafted history')
    if git('cat-file', '-e', base + '^{commit}').returncode:
        raise SyncwheelError('adoption transition base is unavailable')
    parent = base
    for commit in commits:
        result = git('cat-file', '-p', commit)
        parents = [line[7:] for line in result.stdout.split('\n\n', 1)[0].splitlines()
                   if line.startswith('parent ')]
        if result.returncode or parents != [parent]:
            raise SyncwheelError('adoption transition requires a complete linear source sequence')
        parent = commit
    return {'oldTip': old_tip, 'newTip': source_tip, 'base': base}
