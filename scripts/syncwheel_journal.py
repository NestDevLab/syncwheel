"""Journal file admission and safe content observation.

Git status collection and snapshot transactions belong to the caller. Admission
accepts explicit status entries and a replaceable safe file reader.
"""

import fnmatch
import hashlib
import os
import re
import stat
from pathlib import Path

from syncwheel_errors import SyncwheelError

JOURNAL_SENSITIVE_PARTS = {
    '.env', '.ssh', '.gnupg', '.aws', '.kube', '.docker',
    'id_rsa', 'id_ed25519', 'credentials', 'credentials.json',
}

JOURNAL_SECRET_PATTERNS = (
    ('private key', re.compile(br'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----')),
    ('OpenSSH private key', re.compile(br'-----BEGIN OPENSSH PRIVATE KEY-----')),
    ('AWS access key', re.compile(br'(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])')),
    ('GitHub token', re.compile(br'(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{36}(?![A-Za-z0-9])')),
    ('GitHub token', re.compile(br'(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{60,}(?![A-Za-z0-9_])')),
    ('Slack token', re.compile(br'(?<![A-Za-z0-9])xoxb-[0-9]{10,}-[A-Za-z0-9-]{20,}(?![A-Za-z0-9])')),
)


def journal_path_matches(path, patterns):
    path = path.replace(os.sep, '/')
    return any(
        pattern == '**'
        or fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith('**/') and fnmatch.fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def journal_path_sensitive(path):
    parts = Path(path).parts
    return any(part.lower() in JOURNAL_SENSITIVE_PARTS for part in parts)


def journal_secret_reason(content):
    for label, pattern in JOURNAL_SECRET_PATTERNS:
        if pattern.search(content):
            return label
    return None


def journal_file_fingerprint(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SyncwheelError('unsupported file type')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            content = handle.read()
    finally:
        os.close(descriptor)
    return (
        file_stat.st_mode, file_stat.st_size, file_stat.st_mtime_ns,
        file_stat.st_dev, file_stat.st_ino, hashlib.sha256(content).hexdigest(),
    )


def journal_read_admitted_file(path, max_file_bytes):
    path_stat = os.lstat(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise SyncwheelError('unsupported file type')
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise SyncwheelError('content changed before reading')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            before = os.fstat(handle.fileno())
            content = handle.read(max_file_bytes + 1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if len(content) > max_file_bytes or after.st_size > max_file_bytes:
        raise SyncwheelError(f'oversize ({after.st_size} bytes)')
    stable_fields = ('st_mode', 'st_size', 'st_mtime_ns', 'st_ino', 'st_dev')
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SyncwheelError('content changed while reading')
    mode = '100755' if before.st_mode & 0o111 else '100644'
    fingerprint = (
        before.st_mode, before.st_size, before.st_mtime_ns,
        before.st_dev, before.st_ino, hashlib.sha256(content).hexdigest(),
    )
    return content, fingerprint, mode


def parse_status_entries(output):
    tokens = output.split('\0')
    entries = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        status = record[:2]
        path = record[3:]
        if status[0] in {'R', 'C'}:
            if index >= len(tokens):
                raise SyncwheelError('could not parse git status rename record')
            path = tokens[index]
            index += 1
        entries.append({'status': status, 'path': path})
    return entries


def admit_paths(repo_root, journal, entries, paths=None, *, read_file=journal_read_admitted_file):
    admitted = []
    excluded = []
    rejected = []
    fingerprints = {}
    blobs = {}
    for entry in entries:
        path = entry['path']
        if paths is not None and path not in paths:
            continue
        if not journal_path_matches(path, journal['include']) or journal_path_matches(path, journal['exclude']):
            excluded.append(entry)
            continue
        if journal_path_sensitive(path):
            rejected.append({**entry, 'reason': 'sensitive path'})
            continue
        target = repo_root / path
        deleted = entry['status'][1] == 'D' or (entry['status'] == ' D') or not target.exists()
        if deleted:
            fingerprints[path] = None
        else:
            try:
                content, fingerprint, mode = read_file(
                    target, journal['max_file_bytes']
                )
            except (OSError, SyncwheelError) as exc:
                rejected.append({**entry, 'reason': str(exc)})
                continue
            secret = journal_secret_reason(content)
            if secret:
                rejected.append({**entry, 'reason': f'high-confidence secret: {secret}'})
                continue
            fingerprints[path] = fingerprint
            blobs[path] = {'content': content, 'mode': mode}
        admitted.append(entry)
    return admitted, excluded, rejected, fingerprints, blobs
