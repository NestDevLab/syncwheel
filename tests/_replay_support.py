"""Synthetic, hermetic repositories and helpers for replay execution-mode tests."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'
COMMIT_IDENTITY_FORMAT = '%H%n%T%n%an%n%ae%n%aI%n%cn%n%ce%n%cI%n%s%n%b'
EMPTY_COMMIT_POLICY = 'stop'


def hermetic_environment(root):
    """Return an environment that cannot inherit a developer's Git configuration."""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith('GIT_')
    }
    home = Path(root) / 'home'
    home.mkdir(parents=True, exist_ok=True)
    environment.update({
        'HOME': str(home),
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_SYSTEM': os.devnull,
        'PATH': environment.get('PATH', os.defpath),
    })
    return environment


def git(repo_path, *args, expected=0):
    """Run Git in a fixture repository and return its completed process."""
    result = subprocess.run(
        ['git', *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
    )
    if result.returncode != expected:
        raise AssertionError(
            f'git {args} expected {expected}, got {result.returncode}\n'
            f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )
    return result


def configure_repo(repo_path):
    """Pin every repository-local setting that can affect replay identity."""
    for key, value in (
        ('user.name', 'Replay Fixture'),
        ('user.email', 'replay-fixture@example.com'),
        ('commit.gpgsign', 'false'),
        ('rerere.enabled', 'false'),
        ('core.autocrlf', 'false'),
        ('core.fileMode', 'true'),
    ):
        git(repo_path, 'config', key, value)


def run_cli(repo_path, *args, expected=0):
    """Run the current Syncwheel CLI without its self-update side effects."""
    env = dict(os.environ)
    env.update({
        'SYNCWHEEL_UPDATE_MODE': 'off',
        'SYNCWHEEL_REPO_REGISTRY': str(Path(repo_path).parent / 'repos.json'),
        'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(Path(repo_path).parent / 'settings.json'),
    })
    result = subprocess.run(
        ['python3', str(CLI), *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        env=env,
    )
    if result.returncode != expected:
        raise AssertionError(
            f'syncwheel {args} expected {expected}, got {result.returncode}\n'
            f'stdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )
    return result


def commit_log(repo_path, base, branch):
    """Capture every identity-bearing field for the replayed commit range."""
    return git(
        repo_path,
        'log',
        f'--format={COMMIT_IDENTITY_FORMAT}',
        f'{base}..{branch}',
    ).stdout


def load_syncwheel_module():
    spec = importlib.util.spec_from_file_location('syncwheel_replay_modes_under_test', CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_repo(root, name):
    """Create a base repo whose primary checkout is later left on integration."""
    repo_path = Path(root) / name
    repo_path.mkdir()
    git(repo_path, 'init', '-q', '-b', 'main')
    configure_repo(repo_path)
    (repo_path / 'README.md').write_text('replay fixture\n')
    git(repo_path, 'add', 'README.md')
    git(repo_path, 'commit', '-q', '-m', 'chore: base')
    return repo_path, git(repo_path, 'rev-parse', 'HEAD').stdout.strip()


def commit_file(repo_path, path, contents, message):
    target = Path(repo_path) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contents, bytes):
        target.write_bytes(contents)
    else:
        target.write_text(contents)
    git(repo_path, 'add', str(path))
    git(repo_path, 'commit', '-q', '-m', message)
    return git(repo_path, 'rev-parse', 'HEAD').stdout.strip()


def write_manifest(repo_path, base, commits, stack_id='replay'):
    manifest = {
        'version': 1,
        'defaults': {
            'canonical_remote': 'origin',
            'publication_remote': 'origin',
            'base_branch': 'main',
            'base_ref': base,
            'integration_membership': 'required',
        },
        'integration': {
            'branch': 'integration',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [stack_id],
        },
        'stacks': [{
            'id': stack_id,
            'branch': f'pr/{stack_id}',
            'base': base,
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'integration',
            'commits': commits,
        }],
    }
    manifest_path = Path(repo_path) / '.syncwheel' / 'manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def finish_source_scenario(repo_path, base, commits):
    """Leave the primary checkout on integration and return a replay manifest."""
    git(repo_path, 'switch', '-q', '-c', 'integration', base)
    return write_manifest(repo_path, base, commits)


def build_linear_chain(root):
    repo_path, base = create_repo(root, 'linear-chain')
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    first = commit_file(repo_path, 'story.txt', 'one\n', 'feat: add first replay commit')
    second = commit_file(repo_path, 'story.txt', 'one\ntwo\n', 'feat: add second replay commit')
    manifest = finish_source_scenario(repo_path, base, [first, second])
    return repo_path, manifest, 'replay'


def build_moved_base(root):
    repo_path, base = create_repo(root, 'moved-base')
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    first = commit_file(repo_path, 'story.txt', 'one\n', 'feat: add replay commit')
    second = commit_file(repo_path, 'story.txt', 'one\ntwo\n', 'feat: extend replay commit')
    git(repo_path, 'switch', '-q', '-c', 'integration', base)
    moved_base = commit_file(repo_path, 'base-note.txt', 'moved base\n', 'feat: move base')
    manifest = write_manifest(repo_path, moved_base, [first, second])
    return repo_path, manifest, 'replay'


def build_binary_file(root):
    repo_path, base = create_repo(root, 'binary-file')
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    binary = commit_file(
        repo_path,
        'payload.bin',
        b'\x00syncwheel\xff\x10\x00',
        'feat: add binary payload',
    )
    manifest = finish_source_scenario(repo_path, base, [binary])
    return repo_path, manifest, 'replay'


def build_rename(root):
    repo_path, base = create_repo(root, 'rename')
    commit_file(repo_path, 'old-name.txt', 'rename me\n', 'feat: add old name')
    base = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    git(repo_path, 'mv', 'old-name.txt', 'new-name.txt')
    git(repo_path, 'commit', '-q', '-m', 'feat: rename tracked file')
    rename = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    manifest = finish_source_scenario(repo_path, base, [rename])
    return repo_path, manifest, 'replay'


def build_file_mode_change(root):
    repo_path, base = create_repo(root, 'file-mode-change')
    commit_file(repo_path, 'script.sh', '#!/bin/sh\necho replay\n', 'feat: add script')
    base = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    script = repo_path / 'script.sh'
    script.chmod(script.stat().st_mode | 0o111)
    git(repo_path, 'add', 'script.sh')
    git(repo_path, 'commit', '-q', '-m', 'feat: make script executable')
    mode_change = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    manifest = finish_source_scenario(repo_path, base, [mode_change])
    return repo_path, manifest, 'replay'


def build_merge_commit(root):
    repo_path, base = create_repo(root, 'merge-commit')
    git(repo_path, 'switch', '-q', '-c', 'topic', base)
    commit_file(repo_path, 'topic.txt', 'topic\n', 'feat: topic change')
    git(repo_path, 'switch', '-q', '-c', 'integration', base)
    commit_file(repo_path, 'integration.txt', 'integration\n', 'feat: integration change')
    git(repo_path, 'merge', '--no-ff', 'topic', '-m', 'merge topic into integration')
    merge = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    manifest = write_manifest(repo_path, base, [], stack_id='merge-replay')
    return repo_path, manifest, 'merge-replay', base, merge


def build_empty_commit(root):
    repo_path, base = create_repo(root, 'empty-commit')
    git(repo_path, 'switch', '-q', '-c', 'source', base)
    git(repo_path, 'commit', '--allow-empty', '-q', '-m', 'test: empty replay commit')
    empty = git(repo_path, 'rev-parse', 'HEAD').stdout.strip()
    manifest = finish_source_scenario(repo_path, base, [empty])
    return repo_path, manifest, 'replay', base, empty


def clone_repo(repo_path, destination, manifest):
    """Clone a scenario so each replay has separate refs, reflog, and rerere state."""
    subprocess.run(['git', 'clone', '-q', str(repo_path), str(destination)], check=True)
    configure_repo(destination)
    manifest_path = Path(destination) / '.syncwheel' / 'manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    return Path(destination)


def clone_at_base(repo_path, destination, base):
    subprocess.run(['git', 'clone', '-q', str(repo_path), str(destination)], check=True)
    clone = Path(destination)
    configure_repo(clone)
    git(clone, 'checkout', '-q', '--detach', base)
    return clone
