"""Pure, uninstalled candidate for a frozen cherry-pick replay preview.

The caller supplies observed/frozen identities and a complete in-memory Git object
table. No repository adapter, CLI dispatch, apply, object creation or cleanup is
provided. A future adapter must independently collect/recheck every input.

Supported: all declared stacks selected in their declared order; single-parent
source commits; canonical regular-file trees with unchanged paths/modes; exact
preimage replacement. File creation/deletion, renames, symlinks, submodules, empty
commits, alternate integration recipes and implicit content merges are rejected.
Empty root trees round-trip; empty directory entries are rejected rather than
silently flattened. Commit headers use the explicit LF-only grammar in commit();
extension/continuation headers are outside this subset.
An overlapping append needs an explicit byte-preserving witness. That result is
an approved-resolution candidate, NEVER a claim of automatic Git merge parity.
The reported tree is a replay product, not a recovery authorization or commit.
"""

import base64
import hashlib
import json
import re
from pathlib import PurePosixPath


class PreviewError(ValueError):
    """Input is incomplete, drifted, unsupported or not faithfully witnessed."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _decode(value):
    if not isinstance(value, str):
        raise PreviewError("object/witness bytes must be base64 strings")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise PreviewError("invalid base64") from exc
    if base64.b64encode(result).decode("ascii") != value:
        raise PreviewError("noncanonical base64")
    return result


def _encoded(value):
    return base64.b64encode(value).decode("ascii")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreviewError("duplicate manifest key")
        result[key] = value
    return result


def _path(value):
    if (not value or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or "\\" in value or any(ord(c) < 32 for c in value)):
        raise PreviewError("unsupported tree path")
    return value


class _Objects:
    def __init__(self, objects, algorithm):
        if algorithm not in {"sha1", "sha256"}:
            raise PreviewError("unsupported object format")
        self.algorithm = algorithm
        self.width = hashlib.new(algorithm).digest_size
        self.values = {}
        for oid, item in objects.items():
            kind = item["type"]
            if kind not in {"commit", "tree", "blob"}:
                raise PreviewError("unsupported object type")
            data = _decode(item["data"])
            if self.oid(kind, data) != oid:
                raise PreviewError("object identity mismatch")
            self.values[oid] = kind, data

    def oid(self, kind, data):
        return hashlib.new(self.algorithm,
                           kind.encode() + b" " + str(len(data)).encode()
                           + b"\0" + data).hexdigest()

    def read(self, oid, expected):
        if oid not in self.values:
            raise PreviewError("missing object")
        kind, data = self.values[oid]
        if kind != expected:
            raise PreviewError("object type mismatch")
        return data

    def commit(self, oid):
        """Accept tree, parent*, author, committer, blank line, opaque message.

        Headers use LF only and lowercase full-width OIDs. Extensions and all
        continuation headers are unsupported. Identities require a nonempty UTF-8
        name, ASCII email, canonical nonnegative signed-64-bit epoch and HHMM zone
        with hour <= 23 and minute <= 59. Message bytes are not header syntax.
        """
        data = self.read(oid, "commit")
        if b"\n\n" not in data:
            raise PreviewError("invalid commit headers")
        raw = data.split(b"\n\n", 1)[0]
        if any((value < 32 and value != 10) or value == 127 for value in raw):
            raise PreviewError("unsupported commit header separator/control byte")
        headers = raw.split(b"\n")
        if any(line.startswith((b" ", b"\t")) for line in headers):
            raise PreviewError("commit header continuations are unsupported")
        oid_pattern = rb"[0-9a-f]{" + str(self.width * 2).encode() + rb"}"

        def object_header(line, name):
            prefix = name + b" "
            if not line.startswith(prefix) or re.fullmatch(oid_pattern, line[len(prefix):]) is None:
                raise PreviewError("unsupported commit object header/order")
            return line[len(prefix):].decode("ascii")

        tree = object_header(headers[0], b"tree")
        position, parents = 1, []
        while position < len(headers) and headers[position].startswith(b"parent "):
            parents.append(object_header(headers[position], b"parent"))
            position += 1
        if len(parents) != len(set(parents)) or len(headers) != position + 2:
            raise PreviewError("missing, duplicate or unsupported commit headers")
        for label in [b"author", b"committer"]:
            line = headers[position]
            pattern = (label + rb" ([^<>\x00-\x1f\x7f]+) <([^<>\x00-\x20\x7f]+)> "
                       rb"(0|[1-9][0-9]*) ([+-])([0-9]{2})([0-9]{2})")
            match = re.fullmatch(pattern, line)
            if match is None:
                raise PreviewError("missing or unsupported commit identity/order")
            name, email, epoch, _, hour, minute = match.groups()
            try:
                name.decode("utf-8")
                email.decode("ascii")
            except UnicodeError as exc:
                raise PreviewError("unsupported commit identity encoding") from exc
            if name != name.strip(b" ") or int(epoch) > 2**63 - 1 or int(hour) > 23 or int(minute) > 59:
                raise PreviewError("unsupported commit identity value")
            position += 1
        return tree, parents

    def files(self, tree, prefix="", ancestry=()):
        if tree in ancestry:
            raise PreviewError("recursive tree")
        data = self.read(tree, "tree")
        if prefix and not data:
            raise PreviewError("empty directory entries are unsupported")
        result, cursor, names, order = {}, 0, set(), []
        while cursor < len(data):
            space = data.find(b" ", cursor)
            zero = data.find(b"\0", space + 1)
            if space < cursor or zero < 0 or zero + 1 + self.width > len(data):
                raise PreviewError("invalid tree encoding")
            mode, raw_name = data[cursor:space], data[space + 1:zero]
            try:
                name = raw_name.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PreviewError("unsupported filename encoding") from exc
            if "/" in name or name in names:
                raise PreviewError("duplicate or invalid tree entry")
            names.add(name)
            path = _path(prefix + name)
            oid = data[zero + 1:zero + 1 + self.width].hex()
            cursor = zero + 1 + self.width
            order.append(raw_name + (b"/" if mode == b"40000" else b""))
            if mode == b"40000":
                result.update(self.files(oid, path + "/", ancestry + (tree,)))
            elif mode in {b"100644", b"100755"}:
                result[path] = (mode.decode("ascii"), self.read(oid, "blob"))
            else:
                raise PreviewError("unsupported file mode")
        if order != sorted(order):
            raise PreviewError("noncanonical tree ordering")
        if not prefix and self.tree_oid(result) != tree:
            raise PreviewError("tree cannot round-trip without information loss")
        return result

    def tree_oid(self, files):
        root = {}
        for path, value in files.items():
            node = root
            parts = _path(path).split("/")
            for part in parts[:-1]:
                if part in node and not isinstance(node[part], dict):
                    raise PreviewError("file/directory collision")
                node = node.setdefault(part, {})
            if parts[-1] in node:
                raise PreviewError("file/directory collision")
            node[parts[-1]] = value

        def encode(node):
            entries = []
            for name, value in node.items():
                raw = name.encode("utf-8")
                if isinstance(value, dict):
                    mode, oid, key = "40000", encode(value), raw + b"/"
                else:
                    mode, content = value
                    oid, key = self.oid("blob", content), raw
                entries.append((key, mode.encode() + b" " + raw + b"\0"
                                + bytes.fromhex(oid)))
            return self.oid("tree", b"".join(x[1] for x in sorted(entries)))

        return encode(root)


def _append_resolution(before, source_after, current, witness):
    if before is None or source_after is None or current is None:
        raise PreviewError("witness requires an existing regular file")
    if not before[0] == source_after[0] == current[0]:
        raise PreviewError("witness cannot change file mode")
    original, source, actual = before[1], source_after[1], current[1]
    if not original or not original.endswith(b"\n"):
        raise PreviewError("append witness requires a nonempty line boundary")
    if not source.startswith(original) or not actual.startswith(original):
        raise PreviewError("overlap is not a pure append")
    left, right = actual[len(original):], source[len(original):]
    if not left or not right:
        raise PreviewError("append witness requires two nonempty additions")
    required = {"preimage", "sourcePreimage", "sourcePostimage", "postimage",
                "currentAddition", "sourceAddition"}
    if set(witness) != required:
        raise PreviewError("incomplete or unknown witness fields")
    decoded = {k: _decode(v) for k, v in witness.items()}
    if (decoded["preimage"] != actual or decoded["sourcePreimage"] != original
            or decoded["sourcePostimage"] != source
            or decoded["currentAddition"] != left
            or decoded["sourceAddition"] != right):
        raise PreviewError("witness preimage/addition drift")
    after = decoded["postimage"]
    if after not in {original + left + right, original + right + left}:
        raise PreviewError("witness loses, alters or duplicates additions")
    return before[0], after


def decode_replay_manifest(manifest_bytes):
    """Public reader facade; retain every field while validating recipe shape."""
    try:
        manifest = json.loads(manifest_bytes, object_pairs_hook=_unique)
        stacks = manifest["stacks"]
        if manifest["integration"]["strategy"] != "cherry-pick":
            raise PreviewError("unsupported integration strategy")
        if set(manifest["integration"]) - {"base", "branch", "stacks", "strategy"}:
            raise PreviewError("unsupported integration options")
        ids = [s["id"] for s in stacks]
        if not ids or len(set(ids)) != len(ids) or manifest["integration"]["stacks"] != ids:
            raise PreviewError("omitted, reordered or duplicate stack accounting")
        for stack in stacks:
            allowed = {"id", "branch", "base", "target_remote", "target_branch",
                       "integration_branch", "commits", "state", "publication", "meta"}
            if set(stack) - allowed or stack.get("state", "published") != "published":
                raise PreviewError("unsupported alternate stack recipe")
        return manifest
    except PreviewError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
        raise PreviewError("malformed replay manifest") from exc


def validate_object_closure(objects, object_format, commits):
    """Public reader facade for the existing grammar/tree subset, without IO."""
    store = _Objects(objects, object_format)
    for oid in commits:
        store.files(store.commit(oid)[0])


def read_commit_file(objects, object_format, commit, path):
    """Read a regular control file from already verified in-memory objects."""
    store = _Objects(objects, object_format)
    value = store.files(store.commit(commit)[0]).get(path)
    if value is None or value[0] != "100644":
        raise PreviewError("missing or unsupported regular commit file")
    return value[1]


def preview(frozen, observed, objects, witnesses):
    """Compute a deterministic report without any filesystem or subprocess API.

    All inputs, including every opaque manifest field and supplied object, enter
    planDigest. `observed` must be independently collected by a future caller.
    This equality check is NOT itself proof of native repository observations.
    """
    try:
        if _canonical(frozen) != _canonical(observed):
            raise PreviewError("frozen identity/pin/manifest drift")
        required = {"primary", "base", "backup", "backupManifest",
                    "backupManifestSha256", "sourcePins", "objectFormat"}
        if set(frozen) != required:
            raise PreviewError("incomplete or unknown frozen inputs")
        manifest_bytes = _decode(frozen["backupManifest"])
        if hashlib.sha256(manifest_bytes).hexdigest() != frozen["backupManifestSha256"]:
            raise PreviewError("backup manifest digest mismatch")
        manifest = decode_replay_manifest(manifest_bytes)
        stacks = manifest["stacks"]
        pins = frozen["sourcePins"]
        if len({s["branch"] for s in stacks}) != len(stacks) or set(pins) != {s["branch"] for s in stacks}:
            raise PreviewError("incomplete source pin accounting")
        store = _Objects(objects, frozen["objectFormat"])
        # Validate primary and backup closure without modifying either one.
        for key in ["primary", "backup"]:
            files = store.files(store.commit(frozen[key])[0])
            if key == "backup" and files.get(".syncwheel/manifest.json") != ("100644", manifest_bytes):
                raise PreviewError("backup manifest does not match backup tree")
        initial = store.files(store.commit(frozen["base"])[0])
        result, accounting, used = dict(initial), [], set()
        for stack in stacks:
            commits = stack["commits"]
            if not commits or len(set(commits)) != len(commits) or pins[stack["branch"]] != commits[-1]:
                raise PreviewError("empty/duplicate commit list or source pin drift")
            steps = []
            for oid in commits:
                tree, parents = store.commit(oid)
                if len(parents) != 1:
                    raise PreviewError("source commit must have one parent")
                before = store.files(store.commit(parents[0])[0])
                after = store.files(tree)
                layout = lambda files: {p: v[0] for p, v in files.items()}
                if not layout(before) == layout(after) == layout(initial):
                    raise PreviewError("file layout/mode changes are unsupported")
                paths = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
                if not paths:
                    raise PreviewError("empty commits are unsupported")
                removed = {before[p] for p in paths if p not in after}
                added = {after[p] for p in paths if p not in before}
                if removed & added:
                    raise PreviewError("possible rename is unsupported")
                resolutions = []
                for path in paths:
                    key = oid + ":" + path
                    if result.get(path) == before.get(path):
                        if path in after:
                            result[path] = after[path]
                        else:
                            result.pop(path, None)
                    else:
                        if key not in witnesses:
                            raise PreviewError("overlap requires an explicit additive witness")
                        result[path] = _append_resolution(before.get(path), after.get(path), result.get(path), witnesses[key])
                        used.add(key)
                        resolutions.append(path)
                steps.append({"sourcePin": oid, "changedPaths": paths,
                              "explicitResolutionPaths": resolutions})
            accounting.append({"id": stack["id"], "sourcePin": pins[stack["branch"]],
                               "steps": steps})
        if used != set(witnesses):
            raise PreviewError("unused or omitted witness accounting")
        changes = []
        for path in sorted(initial.keys() | result.keys()):
            if initial.get(path) != result.get(path):
                def image(value):
                    return None if value is None else {"mode": value[0], "bytes": _encoded(value[1])}
                changes.append({"path": path, "preimage": image(initial.get(path)),
                                "postimage": image(result.get(path))})
        report = {"schema": "syncwheel.cherry-pick-preview.v1", "stackCount": len(stacks),
                  "accounting": accounting, "changes": changes,
                  "resultTree": store.tree_oid(result),
                  "semantics": "explicit-additive-resolution" if used else "exact-preimage",
                  "applyImplemented": False}
        digest_input = {"frozen": frozen, "observed": observed, "objects": objects,
                        "witnesses": witnesses, "report": report}
        report["planDigest"] = hashlib.sha256(_canonical(digest_input)).hexdigest()
        return report
    except PreviewError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
        raise PreviewError("malformed or unsupported preview input") from exc
