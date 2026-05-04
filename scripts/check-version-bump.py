#!/usr/bin/env python3
import argparse
import subprocess
import sys


RELEASE_RELEVANT_PREFIXES = (
    'githooks/',
    'scripts/',
    'tests/',
)
RELEASE_RELEVANT_FILES = set()
REQUIRED_FILES = {
    'VERSION',
    'CHANGELOG.md',
    'README.md',
}


def git(*args):
    result = subprocess.run(['git', *args], text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def changed_files(base, staged=False):
    if staged:
        output = git('diff', '--cached', '--name-only')
        return {line.strip() for line in output.splitlines() if line.strip()}
    output = git('diff', '--name-only', f'{base}...HEAD')
    return {line.strip() for line in output.splitlines() if line.strip()}


def is_release_relevant(path):
    return path in RELEASE_RELEVANT_FILES or path.startswith(RELEASE_RELEVANT_PREFIXES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--base',
        default='origin/main',
        help='base ref to compare against; defaults to origin/main',
    )
    parser.add_argument(
        '--staged',
        action='store_true',
        help='check staged files for pre-commit usage instead of comparing against --base',
    )
    args = parser.parse_args()

    files = changed_files(args.base, staged=args.staged)
    relevant = sorted(path for path in files if is_release_relevant(path))
    if not relevant:
        print('No release-relevant changes detected.')
        return 0

    missing = sorted(REQUIRED_FILES - files)
    if missing:
        print('Release-relevant changes require a version bump and changelog entry.')
        print('Changed release-relevant files:')
        for path in relevant:
            print(f'  - {path}')
        print('Missing required files:')
        for path in missing:
            print(f'  - {path}')
        return 1

    print('Version bump check passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
