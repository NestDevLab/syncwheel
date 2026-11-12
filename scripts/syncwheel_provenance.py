"""Derived projection identity data, independent of storage and publication."""

import hashlib
import re

from syncwheel_errors import SyncwheelError

def derived_projection_paths_digest(path_blobs):
    """Hash sorted ``path NUL resulting-blob NUL`` records; absence is an empty blob id."""
    digest = hashlib.sha256()
    for path in sorted(path_blobs):
        blob = path_blobs[path]
        if not isinstance(path, str) or not path or '\0' in path:
            raise SyncwheelError('derived projection digest requires non-empty NUL-free paths')
        if blob is not None and (
            not isinstance(blob, str) or not re.fullmatch(r'[0-9a-f]{40,64}', blob)
        ):
            raise SyncwheelError(f'derived projection digest has an invalid blob for {path}')
        digest.update(path.encode('utf-8'))
        digest.update(b'\0')
        digest.update((blob or '').encode('ascii'))
        digest.update(b'\0')
    return digest.hexdigest()
