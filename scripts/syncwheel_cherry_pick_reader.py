"""Bounded read-only acquisition for the pure preview candidate; no CLI/apply.

Supports an ordinary local, non-bare primary worktree with local complete objects.
Linked worktrees, shallow/grafted/replaced/alternate/promisor views, custom Git
environment routing/tracing and unknown repository extensions are rejected.
Only builtin config/ref/object read commands run, with optional locks and Trace2
output disabled, no pager, no replacements and no lazy fetch. There is NO version
allowlist. A caller-supplied backend qualification, bound to executable bytes and
containing direct process-observer evidence, is mandatory before Git starts.
The reader checks evidence consistency, NOT its authenticity: obtaining and
independently approving that evidence is a trusted-caller responsibility. No
index/worktree cleanliness claim is made, and no status/index-refresh, fetch, cleanup or recovery command exists.

Two equal observations detect sampled drift; they do NOT provide an atomic
snapshot, exclude ABA, or protect against arbitrary concurrent/adversarial writers.
The caller must provide a trusted Git executable and cooperative repository/filesystem.
Read access timestamps follow filesystem policy; tests cover bytes/mode/mtime/ctime.
A future caller must bind readDigest/observation scope to its separate apply gate.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from syncwheel_cherry_pick_preview import (
    PreviewError, decode_replay_manifest, read_commit_file, validate_object_closure,
)


class ReaderError(ValueError):
    """Missing, unsupported or drifted native observations."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _qualified_backend(qualification):
    """Validate a trusted caller's local probe evidence; never run a probe here.

    Evidence is not signed/authenticated. This is not authorization to run an
    arbitrary executable: the caller must trust the executable and observer.
    Control must reach a local remote helper; flag-on must attempt no fetch,
    helper or connection. The evidence covers that probe, not arbitrary Git code.
    """
    required = {"schema", "executableSha256", "controlTrace", "disabledTrace",
                "controlHelperStarted", "disabledHelperStarted", "repositoryUnchanged"}
    if not isinstance(qualification, dict) or set(qualification) != required:
        raise ReaderError("explicit backend qualification required; no version qualifies automatically")
    if (qualification["schema"] != "syncwheel.git-backend-probe.v1" or
            qualification["controlHelperStarted"] is not True or
            qualification["disabledHelperStarted"] is not False or
            qualification["repositoryUnchanged"] is not True):
        raise ReaderError("unsupported or unsuccessful backend qualification")
    executable = Path(shutil.which("git") or "").resolve(strict=True)
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    if qualification["executableSha256"] != digest:
        raise ReaderError("backend executable qualification drift")
    control, disabled = (base64.b64decode(qualification[k], validate=True).decode("utf-8")
                         for k in ["controlTrace", "disabledTrace"])
    # Probe trace format: strace -f -e trace=execve,connect -s 8192.
    for trace in [control, disabled]:
        if ('execve(' + json.dumps(str(executable))) not in trace or '"--batch-check"' not in trace:
            raise ReaderError("missing direct backend observer evidence")
    if ('"fetch"' not in control or 'git-remote-preview-probe' not in control or
            'connect(' in control or any(token in disabled for token in
                                       ['"fetch"', 'git-remote-', 'connect('])):
        raise ReaderError("backend probe lacks control or attempted forbidden transport")
    return executable, digest


class _Reader:
    def __init__(self, repository, max_objects, max_bytes, max_tree_entries, qualification):
        if max_objects < 1 or max_bytes < 1 or max_tree_entries < 1:
            raise ReaderError("positive acquisition bounds required")
        self.root = Path(repository).resolve(strict=True)
        self.max_objects, self.max_bytes = max_objects, max_bytes
        self.objects, self.raw, self.commits = {}, {}, set()
        self.total = 0
        self.tree_entries = 0
        self.max_tree_entries = max_tree_entries
        allowed = {"GIT_PAGER", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_OPTIONAL_LOCKS",
                   "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_TERMINAL_PROMPT"}
        if any(key.startswith("GIT_") and key not in allowed for key in os.environ):
            raise ReaderError("unsupported Git environment view or trace output")
        self.executable, self.executable_digest = _qualified_backend(qualification)
        self.qualification_digest = _digest(qualification)
        self.environment = dict(os.environ)
        self.environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
                                GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
                                GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0",
                                LC_ALL="C")
        self.gitdir = self.root / ".git"
        if not self.root.is_dir() or not self.gitdir.is_dir() or self.gitdir.is_symlink():
            raise ReaderError("ordinary non-bare primary worktree required")

    def git(self, *args, data=None, allowed=(0,)):
        if hashlib.sha256(self.executable.read_bytes()).hexdigest() != self.executable_digest:
            raise ReaderError("backend executable changed between observations")
        result = subprocess.run([str(self.executable), "--no-pager", "-C", str(self.root), *args],
                                input=data, capture_output=True, env=self.environment,
                                timeout=20)
        if result.returncode not in allowed or result.stderr:
            raise ReaderError("Git read command failed or produced diagnostics")
        return result.stdout

    def snapshot(self):
        gitdir = self.gitdir
        for path in [gitdir, gitdir / "objects", gitdir / "refs", gitdir / "config",
                     gitdir / "packed-refs", gitdir / "objects" / "pack"]:
            if path.is_symlink():
                raise ReaderError("symlinked Git metadata view unsupported")
        forbidden = ["commondir", "shallow", "info/grafts", "objects/info/alternates",
                     "objects/info/http-alternates"]
        if any((gitdir / p).exists() or (gitdir / p).is_symlink() for p in forbidden):
            raise ReaderError("linked/shallow/grafted/alternate object view unsupported")
        if any((gitdir / "objects" / "pack").glob("*.promisor")):
            raise ReaderError("promisor object view unsupported")
        version = self.git("--version").decode().strip()
        actual = Path(self.git("rev-parse", "--absolute-git-dir").decode().strip()).resolve()
        top = Path(self.git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        if actual != gitdir or top != self.root:
            raise ReaderError("repository identity or worktree view mismatch")
        algorithm = self.git("rev-parse", "--show-object-format").decode().strip()
        if algorithm not in {"sha1", "sha256"}:
            raise ReaderError("unsupported Git object format")
        config = self.git("config", "--null", "--get-regexp",
                          r"^(extensions\.|remote\..*\.(promisor|partialclonefilter)$|core\.(repositoryformatversion|worktree|bare)$)",
                          allowed=(0, 1))
        entries = {}
        for entry in config.split(b"\0"):
            if not entry:
                continue
            key, separator, value = entry.partition(b"\n")
            key, value = key.decode("ascii").lower(), value.decode("ascii")
            if not separator or key in entries:
                raise ReaderError("duplicate or unsupported Git config observation")
            entries[key] = value
            if key == "core.repositoryformatversion" and value in {"0", "1"}:
                continue
            if key == "core.bare" and value == "false":
                continue
            if key == "extensions.objectformat" and value == algorithm:
                continue
            raise ReaderError("unsupported repository extension, routing or promisor config")
        refs = {}
        for line in self.git("for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)").split(b"\n"):
            if not line:
                continue
            pieces = line.split(b"\0")
            if len(pieces) != 3:
                raise ReaderError("malformed native ref observation")
            name, oid, symbolic = (piece.decode("utf-8") for piece in pieces)
            if name.startswith("refs/replace/"):
                raise ReaderError("replacement refs unsupported")
            if name in refs or not name.startswith("refs/"):
                raise ReaderError("duplicate or invalid native ref observation")
            self.check_oid(oid, algorithm)
            refs[name] = {"oid": oid, "symbolic": symbolic}
        head = self.git("rev-parse", "--verify", "HEAD").decode().strip()
        self.check_oid(head, algorithm)
        symbolic = self.git("symbolic-ref", "--quiet", "HEAD", allowed=(0, 1)).decode().strip()
        identity = {}
        for name, path in [("root", self.root), ("git", gitdir), ("objects", gitdir / "objects")]:
            stat = path.stat()
            identity[name] = [stat.st_dev, stat.st_ino]
        return {"gitVersion": version, "backendSha256": self.executable_digest,
                "qualificationDigest": self.qualification_digest,
                "objectFormat": algorithm, "refs": refs,
                "head": {"oid": head, "symbolic": symbolic},
                "viewConfig": entries, "identity": identity}

    @staticmethod
    def check_oid(oid, algorithm):
        width = 40 if algorithm == "sha1" else 64
        if not isinstance(oid, str) or re.fullmatch(r"[0-9a-f]{" + str(width) + "}", oid) is None:
            raise ReaderError("full lowercase object identity required")

    def resolve(self, selector, snapshot, source=False):
        if not isinstance(selector, str) or not selector:
            raise ReaderError("missing ref selector")
        if not source and selector == "HEAD":
            return snapshot["head"]["oid"]
        if not source and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", selector):
            self.check_oid(selector, snapshot["objectFormat"])
            return selector
        if selector.startswith("refs/"):
            candidates = [selector]
        elif source:
            candidates = ["refs/heads/" + selector]
        else:
            candidates = ["refs/heads/" + selector, "refs/remotes/" + selector,
                          "refs/tags/" + selector]
        matches = [(name, snapshot["refs"][name]) for name in candidates if name in snapshot["refs"]]
        if len(matches) != 1:
            raise ReaderError("missing or ambiguous ref selector")
        name, value = matches[0]
        allowed = ("refs/heads/",) if source else ("refs/heads/", "refs/remotes/")
        if not name.startswith(allowed) or value["symbolic"]:
            raise ReaderError("tag or symbolic selected ref unsupported")
        return value["oid"]

    def object(self, oid, kind, algorithm):
        self.check_oid(oid, algorithm)
        if oid in self.raw:
            actual, data = self.raw[oid]
            if actual != kind:
                raise ReaderError("object type mismatch")
            return data
        if len(self.objects) >= self.max_objects:
            raise ReaderError("object acquisition bound exceeded")
        metadata = self.git("cat-file", "--batch-check", data=oid.encode("ascii") + b"\n").strip().split(b" ")
        if len(metadata) != 3 or metadata[:2] != [oid.encode(), kind.encode()]:
            raise ReaderError("missing or unexpected Git object")
        if not metadata[2].isdigit() or self.total + int(metadata[2]) > self.max_bytes:
            raise ReaderError("object byte acquisition bound exceeded")
        output = self.git("cat-file", "--batch", data=oid.encode("ascii") + b"\n")
        header, separator, body = output.partition(b"\n")
        fields = header.split(b" ")
        if not separator or len(fields) != 3 or fields[:2] != [oid.encode(), kind.encode()]:
            raise ReaderError("missing or unexpected Git object")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise ReaderError("invalid native object size") from exc
        if size < 0 or len(body) != size + 1 or not body.endswith(b"\n"):
            raise ReaderError("truncated or malformed native object")
        data = body[:-1]
        if self.total + size > self.max_bytes:
            raise ReaderError("object byte acquisition bound exceeded")
        digest = hashlib.new(algorithm, kind.encode() + b" " + str(size).encode() + b"\0" + data).hexdigest()
        if digest != oid:
            raise ReaderError("native object byte identity mismatch")
        self.total += size
        self.raw[oid] = kind, data
        self.objects[oid] = {"type": kind, "data": base64.b64encode(data).decode("ascii")}
        return data

    def tree(self, oid, algorithm, ancestry=()):
        if oid in ancestry or len(ancestry) > 128:
            raise ReaderError("recursive or unsupported tree depth")
        data = self.object(oid, "tree", algorithm)
        width = 20 if algorithm == "sha1" else 32
        cursor = 0
        while cursor < len(data):
            self.tree_entries += 1
            if self.tree_entries > self.max_tree_entries:
                raise ReaderError("tree entry expansion bound exceeded")
            space, zero = data.find(b" ", cursor), data.find(b"\0", cursor)
            if space < cursor or zero < space or zero + 1 + width > len(data):
                raise ReaderError("malformed tree references")
            mode = data[cursor:space]
            child = data[zero + 1:zero + 1 + width].hex()
            cursor = zero + 1 + width
            if mode == b"40000":
                self.tree(child, algorithm, ancestry + (oid,))
            elif mode in {b"100644", b"100755"}:
                self.object(child, "blob", algorithm)
            else:
                raise ReaderError("unsupported tree entry mode")

    def commit(self, oid, algorithm):
        data = self.object(oid, "commit", algorithm)
        headers = data.partition(b"\n\n")[0].split(b"\n")
        # Dependency discovery only; the shared pure facade validates grammar.
        trees = [line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")]
        parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
        if len(trees) != 1:
            raise ReaderError("missing or ambiguous commit tree")
        for value in trees + parents:
            self.check_oid(value, algorithm)
        self.tree(trees[0], algorithm)
        self.commits.add(oid)
        return parents


def read_snapshot(repository, backup_ref, *, expected=None,
                  max_objects=100_000, max_bytes=64 * 1024 * 1024,
                  max_tree_entries=100_000, qualification=None):
    """Acquire an immutable-object envelope; expected is a prior whole envelope.

    No real recovery or atomicity is implied. Bounds limit accepted output, not
    arbitrary hostile subprocess allocation. Native command failures stop the read.
    """
    try:
        reader = _Reader(repository, max_objects, max_bytes, max_tree_entries, qualification)
        before = reader.snapshot()
        algorithm = before["objectFormat"]
        backup = reader.resolve(backup_ref, before)
        reader.commit(backup, algorithm)
        manifest_bytes = read_commit_file(reader.objects, algorithm, backup, ".syncwheel/manifest.json")
        manifest = decode_replay_manifest(manifest_bytes)
        base_selector = manifest["integration"]["base"]
        base = reader.resolve(base_selector, before)
        for oid in [before["head"]["oid"], base]:
            reader.commit(oid, algorithm)
        pins = {}
        for stack in manifest["stacks"]:
            branch, commits = stack["branch"], stack["commits"]
            if branch in pins or not commits or len(set(commits)) != len(commits):
                raise ReaderError("duplicate or empty recipe accounting")
            pins[branch] = reader.resolve(branch, before, source=True)
            if pins[branch] != commits[-1]:
                raise ReaderError("source ref pin does not match recipe tip")
            for oid in commits:
                parents = reader.commit(oid, algorithm)
                if len(parents) != 1:
                    raise ReaderError("source commit must have one parent")
                reader.commit(parents[0], algorithm)
        validate_object_closure(reader.objects, algorithm, sorted(reader.commits))
        after = reader.snapshot()
        if _canonical(before) != _canonical(after):
            raise ReaderError("sampled repository view/ref drift")
        frozen = {"primary": before["head"]["oid"], "base": base, "backup": backup,
                  "backupManifest": base64.b64encode(manifest_bytes).decode("ascii"),
                  "backupManifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
                  "sourcePins": pins, "objectFormat": algorithm}
        observed = dict(frozen)
        observed["sourcePins"] = {s["branch"]: reader.resolve(s["branch"], after, source=True)
                                  for s in manifest["stacks"]}
        observed.update(primary=after["head"]["oid"], base=reader.resolve(base_selector, after),
                        backup=reader.resolve(backup_ref, after))
        envelope = {"schema": "syncwheel.cherry-pick-reader.v1", "frozen": frozen,
                    "observed": observed, "objects": reader.objects,
                    "observation": {"backupSelector": backup_ref,
                                    "sampleDigest": _digest(before),
                                    "scope": "local immutable objects and sampled refs/views; no atomicity/ABA guarantee",
                                    "workingTreeIndexObserved": False,
                                    "maxObjects": max_objects, "maxBytes": max_bytes,
                                    "maxTreeEntries": max_tree_entries,
                                    "backendQualificationDigest": reader.qualification_digest,
                                    "backendSha256": reader.executable_digest}}
        envelope["readDigest"] = _digest(envelope)
        if expected is not None and _canonical(expected) != _canonical(envelope):
            raise ReaderError("expected envelope drift")
        return envelope
    except ReaderError:
        raise
    except (PreviewError, OSError, subprocess.SubprocessError, KeyError, TypeError,
            ValueError, UnicodeError, AttributeError) as exc:
        raise ReaderError("missing or unsupported reader input/view") from exc
