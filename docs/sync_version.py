#!/usr/bin/env python3
import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = REPO_ROOT / 'VERSION'
INDEX_PATH = REPO_ROOT / 'docs' / 'index.html'
START_MARKER = '<!-- syncwheel-version:start -->'
END_MARKER = '<!-- syncwheel-version:end -->'
EXPECTED_MARKERS = 2


class VersionSyncError(RuntimeError):
    pass


def normalize_version(raw):
    version = raw.strip()
    if not version:
        raise VersionSyncError('VERSION is empty')
    if any(character.isspace() for character in version):
        raise VersionSyncError('VERSION must contain exactly one non-whitespace value')
    return version


def render_html(html, version):
    parts = html.split(START_MARKER)
    marker_count = len(parts) - 1
    end_marker_count = html.count(END_MARKER)
    if marker_count != EXPECTED_MARKERS or end_marker_count != EXPECTED_MARKERS:
        raise VersionSyncError(
            f'expected {EXPECTED_MARKERS} complete version markers in docs/index.html, '
            f'found {marker_count} starts and {end_marker_count} ends'
        )

    rendered = parts[0]
    for part in parts[1:]:
        _, separator, remainder = part.partition(END_MARKER)
        if not separator:
            raise VersionSyncError('version marker is missing its end marker in docs/index.html')
        rendered += f'{START_MARKER}{version}{END_MARKER}{remainder}'
    return rendered


def read_staged(path):
    relative_path = path.relative_to(REPO_ROOT).as_posix()
    result = subprocess.run(
        ['git', 'show', f':{relative_path}'],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise VersionSyncError(f'cannot read staged {relative_path}: {message}')
    return result.stdout


def load_inputs(staged=False):
    if staged:
        return read_staged(VERSION_PATH), read_staged(INDEX_PATH)
    return VERSION_PATH.read_text(), INDEX_PATH.read_text()


def main():
    parser = argparse.ArgumentParser(
        description='Render the static website version from the root VERSION file.'
    )
    parser.add_argument(
        '--check',
        action='store_true',
        help='fail instead of writing when docs/index.html is out of date',
    )
    parser.add_argument(
        '--staged',
        action='store_true',
        help='check the Git index instead of the working tree (requires --check)',
    )
    args = parser.parse_args()

    if args.staged and not args.check:
        parser.error('--staged requires --check')

    try:
        raw_version, html = load_inputs(staged=args.staged)
        version = normalize_version(raw_version)
        rendered = render_html(html, version)
    except (OSError, VersionSyncError) as exc:
        print(f'Website version sync failed: {exc}', file=sys.stderr)
        return 1

    label = 'staged docs/index.html' if args.staged else 'docs/index.html'
    if rendered == html:
        print(f'{label} matches VERSION ({version}).')
        return 0

    if args.check:
        if args.staged:
            instruction = 'stage VERSION and docs/index.html together'
        else:
            instruction = 'run python3 docs/sync_version.py'
        print(
            f'{label} does not match VERSION ({version}); {instruction}.',
            file=sys.stderr,
        )
        return 1

    INDEX_PATH.write_text(rendered)
    print(f'Updated docs/index.html to VERSION ({version}).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
