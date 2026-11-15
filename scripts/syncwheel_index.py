"""Git index binary decoding and semantic content leases.

Byte parsing is independent of Git invocation and index I/O."""

import hashlib
import struct


INDEX_OBJECT_FORMATS = {'sha1': (20, hashlib.sha1), 'sha256': (32, hashlib.sha256)}


INDEX_CACHE_ONLY_EXTENSIONS = frozenset({b'TREE', b'UNTR', b'FSMN', b'EOIE', b'IEOT'})


INDEX_SEMANTIC_DIGEST_DOMAIN = b'syncwheel-index-semantic-v1\0'


INDEX_ENTRY_ASSUME_VALID = 0x8000


INDEX_ENTRY_EXTENDED = 0x4000


INDEX_ENTRY_NAME_MASK = 0x0FFF


def _index_varint(payload, offset, end):
    if offset >= end:
        return None, offset
    byte = payload[offset]
    offset += 1
    value = byte & 0x7F
    while byte & 0x80:
        if offset >= end:
            return None, offset
        byte = payload[offset]
        offset += 1
        value = ((value + 1) << 7) | (byte & 0x7F)
    return value, offset


def parse_index_semantics(payload, object_format):
    """Return the staged content of a Git index file, without stat data.

    Returns None whenever the bytes alone cannot prove what Git would stage:
    unknown object format or version, a required extension (split or sparse
    index), truncation, or a trailer checksum mismatch.
    """
    spec = INDEX_OBJECT_FORMATS.get(object_format)
    if spec is None:
        return None
    oid_size, hash_factory = spec
    if len(payload) < 12 + oid_size or payload[:4] != b'DIRC':
        return None
    end = len(payload) - oid_size
    if hash_factory(payload[:end]).digest() != payload[end:]:
        return None
    version, count = struct.unpack_from('>II', payload, 4)
    if version not in (2, 3, 4):
        return None
    fixed_size = 40 + oid_size + 2
    offset = 12
    previous_path = b''
    entries = []
    for _ in range(count):
        start = offset
        if offset + fixed_size > end:
            return None
        (mode,) = struct.unpack_from('>I', payload, offset + 24)
        oid = payload[offset + 40:offset + 40 + oid_size]
        (flags,) = struct.unpack_from('>H', payload, offset + 40 + oid_size)
        offset += fixed_size
        extended = 0
        if flags & INDEX_ENTRY_EXTENDED:
            if version < 3 or offset + 2 > end:
                return None
            (extended,) = struct.unpack_from('>H', payload, offset)
            offset += 2
        if version == 4:
            strip, offset = _index_varint(payload, offset, end)
            if strip is None or strip > len(previous_path):
                return None
            terminator = payload.find(b'\0', offset, end)
            if terminator < 0:
                return None
            path = previous_path[:len(previous_path) - strip] + payload[offset:terminator]
            offset = terminator + 1
        else:
            terminator = payload.find(b'\0', offset, end)
            if terminator < 0:
                return None
            path = payload[offset:terminator]
            padded = start + ((terminator - start + 8) & ~7)
            if padded > end or payload[terminator:padded].strip(b'\0'):
                return None
            offset = padded
        if not path or flags & INDEX_ENTRY_NAME_MASK != min(len(path), INDEX_ENTRY_NAME_MASK):
            return None
        previous_path = path
        entries.append((
            path,
            mode,
            oid,
            (flags >> 12) & 0x3,
            bool(flags & INDEX_ENTRY_ASSUME_VALID),
            extended,
        ))
    extensions = []
    while offset < end:
        if offset + 8 > end:
            return None
        signature = payload[offset:offset + 4]
        (size,) = struct.unpack_from('>I', payload, offset + 4)
        data_end = offset + 8 + size
        if data_end > end:
            return None
        # only 'A'..'Z' extensions are optional, others (link, sdir) change
        # how the entries have to be read
        if not 0x41 <= signature[0] <= 0x5A:
            return None
        if signature not in INDEX_CACHE_ONLY_EXTENSIONS:
            extensions.append((signature, payload[offset + 8:data_end]))
        offset = data_end
    return {'version': version, 'entries': entries, 'extensions': extensions}


def index_semantic_digest(payload, object_format):
    """Digest what an index stages, independent of stat fields and version."""
    parsed = parse_index_semantics(payload, object_format)
    if parsed is None:
        return None
    digest = hashlib.sha256(
        INDEX_SEMANTIC_DIGEST_DOMAIN + object_format.encode('ascii') + b'\0'
    )
    digest.update(struct.pack('>I', len(parsed['entries'])))
    for path, mode, oid, stage_number, assume_valid, extended in parsed['entries']:
        digest.update(struct.pack('>I', len(path)) + path)
        digest.update(struct.pack('>I', mode) + oid)
        digest.update(struct.pack('>BBH', stage_number, assume_valid, extended))
    for signature, data in parsed['extensions']:
        digest.update(signature + struct.pack('>Q', len(data)) + data)
    return digest.hexdigest()


class IndexLease:
    __slots__ = ('sha256', 'semantic')

    def __init__(self, sha256, semantic):
        self.sha256 = sha256
        self.semantic = semantic


class IndexObservation:
    """One read of the real index, semantic digest parsed on demand."""

    def __init__(self, payload, object_format):
        self.payload = payload
        self.object_format = object_format
        self.sha256 = hashlib.sha256(payload).hexdigest()
        self._semantic = None
        self._parsed = False

    @property
    def semantic(self):
        if not self._parsed:
            self._semantic = (
                index_semantic_digest(self.payload, self.object_format)
                if self.object_format else None
            )
            self._parsed = True
        return self._semantic
