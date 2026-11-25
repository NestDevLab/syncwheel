"""Optimistic JSON documents with cooperative locking and exact-byte CAS.

Read snapshots retain their preimage through deepcopy. A stale writer must
re-read and reapply its intent; atomic replacement alone cannot prevent loss.
"""

import json
from pathlib import Path

from syncwheel_errors import SyncwheelError


class JsonSnapshot(dict):
    def __init__(self, value, path, preimage):
        super().__init__(value)
        self.path = str(Path(path).absolute())
        self.preimage = preimage


def observed_bytes(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def load_document(path, default, *, invalid_label='invalid JSON file',
                  root_label='JSON root must be an object'):
    payload = observed_bytes(path)
    if payload is None:
        return JsonSnapshot(default, path, None) if isinstance(default, dict) else default
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SyncwheelError(f'{invalid_label}: {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise SyncwheelError(f'{root_label}: {path}')
    return JsonSnapshot(value, path, payload)


def save_document(path, document, *, lock, write_json):
    path = Path(path)
    identity = str(path.absolute())
    if isinstance(document, JsonSnapshot):
        if document.path != identity:
            raise SyncwheelError('private JSON snapshot belongs to a different path')
        expected = document.preimage
    else:
        # An explicitly supplied replacement has no prior read to preserve.
        # Still freeze it before entering the lock so a racing save refuses.
        expected = observed_bytes(path)
    with lock(path):
        if observed_bytes(path) != expected:
            raise SyncwheelError(
                f'private JSON configuration changed concurrently: {path}; '
                're-read and reapply the requested change'
            )
        result = write_json(path, document)
        if isinstance(document, JsonSnapshot):
            document.preimage = observed_bytes(path)
        return result


def configuration_busy_error(metadata, stale):
    return SyncwheelError('private JSON configuration is busy; retry after its writer finishes')
