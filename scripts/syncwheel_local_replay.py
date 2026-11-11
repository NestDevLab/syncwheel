"""Witnessed local adoption for a deliberately restricted checkout shape."""

import contextlib
import copy
import ctypes
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # The backend is intentionally Linux-only.
    fcntl = None


MANIFEST = ".syncwheel/manifest.json"
OID = re.compile(r"[0-9a-f]{40}")
HASH64 = re.compile(r"[0-9a-f]{64}")
SAFE_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
MAX_ENTRIES = 10000
MAX_BYTES = 32 * 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ExportFileHandle(ctypes.Structure):
    """Linux struct file_handle, with the bounded MAX_HANDLE_SZ payload."""
    _fields_ = [("handle_bytes", ctypes.c_uint), ("handle_type", ctypes.c_int),
                ("data", ctypes.c_ubyte * 128)]


class LocalReplayError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise LocalReplayError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def blob_oid(data):
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def checkpoint(phase):
    """Test seam; no environment-variable-controlled failpoint."""


def git_env(extra=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
        GIT_ALLOW_PROTOCOL="", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat",
    )
    env.update(extra or {})
    return env


def require_ambient_location():
    require(not any(k.startswith(("GIT_CONFIG", "GIT_OBJECT")) or k in {
        "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    } for k in os.environ), "local replay refuses ambient Git location/configuration overrides")


@contextlib.contextmanager
def sanitized_cli_environment():
    """The dedicated CLI enters here before repository resolution or other Git."""
    require_ambient_location()
    original = os.environ.copy()
    cleaned = git_env()
    os.environ.clear()
    os.environ.update(cleaned)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def git(root, *args, data=None, env=None, allowed=(0,)):
    result = subprocess.run(
        ["git", "-c", "core.fsync=loose-object,index,reference", *args],
        cwd=root, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env(env),
    )
    require(result.returncode in allowed, f"local replay Git {args[0]} failed: "
            + result.stderr.decode(errors="replace").strip()[:512])
    return result.stdout


def oid(root, rev):
    value = git(root, "rev-parse", "--verify", "--end-of-options", rev).decode().strip()
    require(OID.fullmatch(value), "local replay requires SHA-1 objects")
    return value


def symbolic(root, ref):
    return git(root, "symbolic-ref", "-q", ref, allowed=(0, 1)).decode().strip()


def head(root, ref):
    require(SAFE_REF.fullmatch(ref) and not symbolic(root, ref), "local replay requires a direct branch ref")
    return oid(root, ref + "^{commit}")


def safe_path(path):
    return bool(SAFE_PATH.fullmatch(path) and all(
        part.lower() not in {"", ".", "..", ".git"} for part in path.split("/")
    ))


@contextlib.contextmanager
def directory(path, parent=None):
    fd = os.open(path, DIR_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def parent_dir(fd, path):
    require(safe_path(path), "local replay found an unsupported path")
    with contextlib.ExitStack() as stack:
        for part in path.split("/")[:-1]:
            fd = stack.enter_context(directory(part, fd))
        yield fd, path.rsplit("/", 1)[-1]


def file_image(fd, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_BYTES
                and before.st_nlink == 1 and not before.st_mode & 0o7022
                and not os.listxattr(stream.fileno()),
                "local replay requires bounded regular files without aliases or extended attributes")
        data = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        at_name = os.stat(name, dir_fd=fd, follow_symlinks=False)
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode, s.st_mtime_ns, s.st_ctime_ns)
    require(identity(before) == identity(after) == identity(at_name) and len(data) == before.st_size,
            "local replay file changed during observation")
    return data, stat.S_IMODE(before.st_mode)


def image(fd, name):
    data, mode = file_image(fd, name)
    return {"sha256": digest(data), "mode": mode}


def tree(root, commit):
    result, directories, parents, seen = {}, set(), set(), set()
    for row in git(root, "ls-tree", "-r", "-t", "-z", commit).split(b"\0")[:-1]:
        meta, raw_path = row.split(b"\t", 1)
        mode, kind, object_id = meta.decode().split()
        path = raw_path.decode("ascii", errors="replace")
        require(safe_path(path) and OID.fullmatch(object_id) and path not in seen,
                "local replay found an invalid or duplicate tree path")
        seen.add(path)
        require(len(seen) <= MAX_ENTRIES, "local replay tree exceeds the entry limit")
        if kind == "tree" and mode == "040000":
            directories.add(path)
            continue
        require(mode in {"100644", "100755"} and kind == "blob",
                "local replay supports only regular tracked paths")
        result[path] = [mode, object_id]
        ancestor = path
        while "/" in ancestor:
            ancestor = ancestor.rsplit("/", 1)[0]
            parents.add(ancestor)
    require(directories == parents, "local replay cannot preserve empty subtrees through the index")
    return result


def require_no_attributes(root, expected):
    pathlist = b"".join(path.encode("ascii") + b"\0" for path in sorted(expected))
    require(not git(root, "check-attr", "--all", "-z", "--stdin", data=pathlist),
            "local replay refuses effective checkout attributes")
    directories = {""}
    for path in expected:
        while "/" in path:
            path = path.rsplit("/", 1)[0]
            directories.add(path)
    require(not any(os.path.lexists(root / path / ".gitattributes") for path in directories),
            "local replay refuses checkout .gitattributes files")


def require_clean_index(root, expected):
    entries = {}
    for row in git(root, "ls-files", "-v", "--stage", "-z").split(b"\0")[:-1]:
        meta, path = row.split(b"\t", 1)
        flag, mode, object_id, stage = meta.decode().split()
        require(flag == "H" and stage == "0", "local replay requires a full, conflict-free index without hidden entries")
        entries[path.decode()] = [mode, object_id]
    require(entries == expected, "local replay index differs from the expected tree")


def checkout(root_fd, expected):
    observed = {}
    total = 0
    for path in sorted(expected):
        mode, object_id = expected[path]
        with parent_dir(root_fd, path) as (parent, name):
            data, permissions = file_image(parent, name)
        require(blob_oid(data) == object_id and bool(permissions & 0o100) == (mode == "100755"),
                f"local replay checkout differs at {path}")
        total += len(data)
        require(total <= 8 * MAX_BYTES, "local replay checkout exceeds the total byte limit")
        observed[path] = {"sha256": digest(data), "mode": permissions}
    return observed


def addition_roots(baseline, changes):
    """Group new subtrees at their first absent tracked directory boundary."""
    directories = set()
    for path in baseline:
        parts = path.split("/")
        directories.update("/".join(parts[:i]) for i in range(1, len(parts)))
    result = {}
    for path, change in sorted(changes.items()):
        if change["before"] is not None:
            continue
        parts = path.split("/")
        missing = next(("/".join(parts[:i]) for i in range(1, len(parts))
                        if "/".join(parts[:i]) not in directories), None)
        root = missing or path
        group = result.setdefault(root, {"kind": "directory" if missing else "file", "paths": []})
        group["paths"].append(path)
    return result


def added_image(parent, name, kind):
    """Observe one new inode without following links or accepting special files."""
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if kind == "file":
        return image(parent, name)
    require(kind == "directory" and stat.S_ISDIR(info.st_mode),
            "local replay addition changed kind")
    entries, directories = {}, {}
    total = 0

    def visit(fd, prefix):
        nonlocal total
        details = os.fstat(fd)
        require(details.st_uid == os.geteuid() and stat.S_IMODE(details.st_mode) == 0o755
                and not os.listxattr(fd), "local replay addition directory changed")
        directories[prefix] = 0o755
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for child in names:
            relative = f"{prefix}/{child}" if prefix else child
            require(safe_path(relative) and len(entries) + len(directories) <= MAX_ENTRIES,
                    "local replay addition exceeds its bounds")
            item = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(item.st_mode):
                with directory(child, fd) as nested:
                    visit(nested, relative)
            else:
                data, permissions = file_image(fd, child)
                total += len(data)
                require(total <= 8 * MAX_BYTES, "local replay addition exceeds its byte limit")
                entries[relative] = {"sha256": digest(data), "mode": permissions}

    with directory(name, parent) as fd:
        visit(fd, "")
    return {"kind": "directory", "entries": entries, "directories": directories}


def native_handle(parent, name):
    """Opaque filesystem identity includes generation, unlike reusable inode numbers."""
    api = getattr(ctypes.CDLL(None, use_errno=True), "name_to_handle_at", None)
    require(api is not None, "local replay additions require native filesystem handles")
    api.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    api.restype = ctypes.c_int
    handle = ExportFileHandle()
    handle.handle_bytes = 128
    mount_id = ctypes.c_int()
    # No symlink following and no open_by_handle_at, privilege or fallback.
    result = api(parent, os.fsencode(name), ctypes.byref(handle), ctypes.byref(mount_id), 0)
    require(result == 0 and 0 < handle.handle_bytes <= 128 and handle.handle_type >= 0,
            "local replay additions require supported native filesystem handles")
    return {"type": handle.handle_type, "value": bytes(handle.data[:handle.handle_bytes]).hex()}


def valid_native_handle(value):
    return (type(value) is dict and set(value) == {"type", "value"}
            and type(value["type"]) is int and value["type"] >= 0
            and type(value["value"]) is str and re.fullmatch(r"(?:[0-9a-f]{2}){1,128}", value["value"]) is not None)


def added_identity(parent, name, kind):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    result = {"device": info.st_dev, "inode": info.st_ino, "fileHandle": native_handle(parent, name)}
    if kind == "file":
        require(stat.S_ISREG(info.st_mode), "local replay addition changed kind")
        return result
    require(kind == "directory" and stat.S_ISDIR(info.st_mode), "local replay addition changed kind")
    descendants = {}

    def visit(fd, prefix):
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for child in names:
            relative = f"{prefix}/{child}" if prefix else child
            require(safe_path(relative) and len(descendants) < MAX_ENTRIES,
                    "local replay addition identity exceeds its bounds")
            item = os.stat(child, dir_fd=fd, follow_symlinks=False)
            require(item.st_dev == info.st_dev and (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode)),
                    "local replay addition identity changed kind or filesystem")
            child_kind = "directory" if stat.S_ISDIR(item.st_mode) else "file"
            descendants[relative] = {"device": item.st_dev, "inode": item.st_ino, "kind": child_kind,
                                     "fileHandle": native_handle(fd, child)}
            if child_kind == "directory":
                with directory(child, fd) as nested:
                    opened = os.fstat(nested)
                    require((opened.st_dev, opened.st_ino) == (item.st_dev, item.st_ino),
                            "local replay addition inode changed during observation")
                    visit(nested, relative)

    with directory(name, parent) as fd:
        opened = os.fstat(fd)
        require((opened.st_dev, opened.st_ino) == (info.st_dev, info.st_ino),
                "local replay addition inode changed during observation")
        visit(fd, "")
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    require((after.st_dev, after.st_ino) == (info.st_dev, info.st_ino),
            "local replay addition inode changed during observation")
    require(native_handle(parent, name) == result["fileHandle"],
            "local replay addition native identity changed during observation")
    return {**result, "descendants": descendants}


def expected_addition(root, group, entries, repo):
    images, directories = {}, {"": 0o755}
    for path in group["paths"]:
        mode, object_id = entries[path]
        size = int(git(repo, "cat-file", "-s", object_id).strip())
        require(0 <= size <= MAX_BYTES, "local replay source blob exceeds its byte limit")
        data = git(repo, "cat-file", "blob", object_id)
        item = {"sha256": digest(data), "mode": 0o755 if mode == "100755" else 0o644}
        if group["kind"] == "file":
            return item
        relative = path[len(root) + 1:]
        images[relative] = item
        parts = relative.split("/")
        directories.update(("/".join(parts[:i]), 0o755) for i in range(1, len(parts)))
    return {"kind": "directory", "entries": images, "directories": directories}


def hook_observation(root, api):
    hooks, custom = api.active_hooks_dir(root)
    require(custom is None and not hooks.is_symlink(),
            "local replay does not support a custom hooks directory")
    hook = hooks / "reference-transaction"
    chain = hooks / "reference-transaction.syncwheel-chain"
    require(not chain.exists() and not chain.is_symlink() and not hook.is_symlink(),
            "local replay does not support chained or symbolic reference hooks")
    if not hook.exists():
        require(not os.path.lexists(api.primary_guard_path(root)),
                "local replay refuses a configured primary guard without its reference hook")
        return None
    require(hook.is_file() and os.access(hook, os.X_OK)
            and api.managed_hook_status(root, "reference-transaction")["ready"],
            "local replay requires the exact installed managed reference hook")
    return digest(hook.read_bytes())


def require_local_objects(root, git_dir):
    objects = git_dir / "objects"
    require(objects.is_dir() and not objects.is_symlink(),
            "local replay refuses a symbolic or external object store")
    for arguments, expected in (
        (("--absolute-git-dir",), git_dir),
        (("--path-format=absolute", "--git-common-dir"), git_dir),
        (("--path-format=absolute", "--git-path", "objects"), objects),
    ):
        actual = Path(os.fsdecode(git(root, "rev-parse", *arguments)).rstrip("\n"))
        require(actual == expected, "local replay requires the exact private Git directory and object store")
    # Git can follow a symlink at a loose object fan-out, pack or info path.
    # Check the complete local store without following directory links.
    def mount_id(fd):
        for row in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines():
            if row.startswith("mnt_id:"):
                return row.split()[1]
        raise LocalReplayError("local replay cannot attest the object store mount")

    def walk_error(exc):
        raise exc

    with directory(git_dir) as fd:
        expected_mount = mount_id(fd)
    for parent, directories, files in os.walk(objects, followlinks=False, onerror=walk_error):
        with directory(parent) as fd:
            require(mount_id(fd) == expected_mount, "local replay refuses a separately mounted object store")
        require(not any((Path(parent) / entry).is_symlink() for entry in (*directories, *files)),
                "local replay refuses a symbolic entry in the object store")


def topology(root, api):
    require(sys.platform == "linux" and fcntl is not None,
            "local replay requires the Linux files backend")
    require_ambient_location()
    git_dir = root / ".git"
    # An explicit files declaration is the supported backend, not an unsupported
    # extension. Older Git echoes this unknown rev-parse flag; it only supports
    # files refs, and the separate extension check still rejects reftable.
    ref_storage = git(root, "config", "--get", "extensions.refStorage", allowed=(0, 1)).strip()
    ref_format = git(root, "rev-parse", "--show-ref-format").strip()
    require(ref_storage in {b"", b"files"} and ref_format in {b"files", b"--show-ref-format"},
            "local replay requires the files reference backend")
    require(git_dir.is_dir() and not git_dir.is_symlink()
            and git(root, "rev-parse", "--show-toplevel").decode().strip() == str(root)
            and git(root, "rev-parse", "--show-object-format").decode().strip() == "sha1"
            and git(root, "rev-parse", "--is-shallow-repository").decode().strip() == "false"
            and not git(root, "config", "--get-regexp",
                        r"^(extensions\.partialclone|remote\..*\.promisor|core\.(splitindex|sparsecheckout|worktree|fsmonitor|autocrlf|eol|attributesfile)|index\.sparse|filter\..+)$",
                        allowed=(0, 1)),
            "local replay requires the primary checkout of a full SHA-1 files repository")
    require_local_objects(root, git_dir)
    require(not any(os.path.lexists(git_dir / item) for item in (
        "objects/info/alternates", "info/grafts", "info/attributes", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
        "rebase-apply", "rebase-merge", "sequencer",
    )), "local replay refuses alternates or an interrupted Git operation")
    return git_dir


def make_plan(root, stack_id, api):
    git_dir = topology(root, api)
    manifest_path = root / MANIFEST
    manifest, _ = api.load_manifest(root, manifest_path)
    require(manifest and manifest.get("syncwheel_tracking") == "git-tracked"
            and manifest.get("repository_mode", "delivery") == "delivery"
            and not api.coordination_is_active(manifest),
            "local replay requires a tracked, non-coordinated delivery manifest")
    authority = api.manifest_authority(manifest)
    require(authority["mode"] == api.AUTHORITY_MODE_AI_MANAGED
            and "source_change" in authority["allow"] and "source_change" not in authority["deny"],
            "local replay requires declared source-change authority")
    integration = manifest["integration"]
    require(integration.get("strategy", "cherry-pick") == "cherry-pick",
            "local replay supports cherry-pick integration only")
    stack = api.require_stack(manifest, stack_id)
    require(stack_id in integration["stacks"] and not any(stack.get(key) for key in (
        "integration_only_commits", "integration_commits", "integration_resolution",
    )), "local replay requires an integration member without a previous integration binding")
    target = "refs/heads/" + integration["branch"]
    source = "refs/heads/" + stack["branch"]
    require(source != target and symbolic(root, "HEAD") == target,
            "local replay requires integration checked out and a distinct source branch")
    before, source_tip = head(root, target), head(root, source)
    require(all(OID.fullmatch(pin) for pin in [stack["base"], integration["base"], *stack["commits"]]),
            "local replay requires exact commit IDs for bases and source commits")
    base = oid(root, stack["base"] + "^{commit}")
    integration_base = oid(root, integration["base"] + "^{commit}")
    require(git(root, "merge-base", base, before).decode().strip() == base
            and git(root, "merge-base", integration_base, before).decode().strip() == integration_base,
            "local replay bases must be ancestors of integration")
    commits = [oid(root, item + "^{commit}") for item in stack["commits"]]
    require(0 < len(commits) <= 200 and commits[-1] == source_tip,
            "local replay source tip is not the exact declared stack tip")
    parent = base
    for commit in commits:
        require(git(root, "rev-list", "--parents", "-n", "1", commit).decode().split() == [commit, parent],
                "local replay requires the exact linear source sequence")
        parent = commit
    for other in manifest["stacks"]:
        if other["id"] != stack_id:
            owned = {*other["commits"], *other.get("integration_commits", []),
                     *other.get("integration_only_commits", [])}
            require(all(OID.fullmatch(pin) for pin in owned),
                    "local replay requires exact commit ownership throughout the manifest")
            require(not set(commits).intersection(oid(root, item + "^{commit}") for item in owned),
                    "local replay source commit is declared by another stack")
    baseline, original, wanted = tree(root, before), tree(root, base), tree(root, source_tip)
    require(not any(path.rsplit("/", 1)[-1] == ".gitattributes" for path in baseline | original | wanted),
            "local replay refuses checkout attributes and filters")
    require(original.keys() <= wanted.keys(), "local replay refuses source deletions")
    changes = {path: {"before": original.get(path), "after": wanted[path]} for path in wanted
               if original.get(path) != wanted[path]}
    require(changes and not any(path.startswith(".syncwheel/") or path in {".gitignore", ".gitattributes"}
                                for path in changes),
            "local replay requires a nonempty product-only source delta")
    for path, entry in changes.items():
        require((entry["before"] is None or entry["before"][0] == entry["after"][0])
                and baseline.get(path) == entry["before"],
                f"local replay conflict or unsupported mode change at {path}")
    changed_bytes = 0
    for entry in changes.values():
        size = int(git(root, "cat-file", "-s", entry["after"][1]).strip())
        require(0 <= size <= MAX_BYTES, "local replay source blob exceeds its byte limit")
        changed_bytes += size
        require(changed_bytes <= 8 * MAX_BYTES, "local replay source delta exceeds its byte limit")
    additions = addition_roots(baseline, changes)
    require(baseline.get(MANIFEST, [None])[0] == "100644",
            "local replay requires a tracked control manifest")
    require_no_attributes(root, baseline)
    require_clean_index(root, baseline)
    with directory(root) as work, directory(git_dir) as meta:
        # The complete tracked checkout is verified without filters or optional index updates.
        verified = checkout(work, baseline)
        observed = {path: verified[path] for path in sorted({*changes, MANIFEST}) if path in baseline}
        if additions:
            # Refuse unsupported filesystems before preparing objects or journals.
            native_handle(work, ".")
            native_handle(meta, ".")
            native_handle(meta, "index")
        for path in additions:
            with parent_dir(work, path) as (parent, name):
                native_handle(parent, ".")
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise LocalReplayError("local replay addition collides with existing checkout data")
        committed = json.loads(git(root, "cat-file", "blob", baseline[MANIFEST][1]))
        require(api.normalize_manifest_data(committed, manifest_path) == manifest,
                "local replay manifest changed during planning")
        index = image(meta, "index")
    payload = {
        "schemaVersion": 1, "kind": "localIntegrationReplayPlan", "stack": stack_id,
        "targetRef": target, "integrationTip": before, "integrationBase": integration_base,
        "sourceRef": source, "sourceTip": source_tip, "sourceBase": base, "commits": commits,
        "manifestDigest": api.manifest_digest(manifest), "index": index,
        "referenceHook": hook_observation(root, api),
        "witness": {"method": "exact-path-preimages", "paths": changes, "checkout": observed,
                    "additions": additions},
    }
    require(symbolic(root, "HEAD") == target and head(root, target) == before and head(root, source) == source_tip,
            "local replay refs changed during planning")
    payload["planDigest"] = api.canonical_json_digest(payload)
    return payload, manifest


def prepared_objects(root, stage, plan, manifest, api):
    index = stage / "prepared-index"
    env = {"GIT_INDEX_FILE": str(index)}
    old = plan["integrationTip"]
    git(root, "read-tree", old, env=env)
    product = tree(root, old)
    for path, value in plan["witness"]["paths"].items():
        mode, blob = value["after"]
        git(root, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        product[path] = [mode, blob]

    date = git(root, "show", "-s", "--format=%ct", old).decode().strip()
    identity = {
        f"GIT_{role}_{key}": value for role in ("AUTHOR", "COMMITTER")
        for key, value in (("NAME", "Syncwheel"), ("EMAIL", "syncwheel@example.invalid"),
                           ("DATE", f"@{date} +0000"))
    }

    def commit(parent, message):
        tree_id = git(root, "write-tree", env=env).decode().strip()
        return git(root, "-c", "commit.gpgsign=false", "commit-tree", tree_id,
                   "-p", parent, "-m", message, env=identity).decode().strip()

    product_commit = commit(old, "Project witnessed local source onto integration")
    require(tree(root, product_commit) == product, "local replay product tree differs from its witness")
    updated = copy.deepcopy(manifest)
    stack = api.require_stack(updated, plan["stack"])
    stack["integration_commits"] = [product_commit]
    stack["integration_resolution"] = {"source_tip": plan["sourceTip"]}
    encoded = api.canonical_manifest_file_text(updated).encode()
    blob = git(root, "hash-object", "-w", "--stdin", data=encoded).decode().strip()
    git(root, "update-index", "--cacheinfo", f"100644,{blob},{MANIFEST}", env=env)
    final = commit(product_commit, "Record witnessed local integration projection")
    product[MANIFEST] = ["100644", blob]
    require(tree(root, final) == product, "local replay control tree differs from its witness")
    return product_commit, final


def create_file(directory_path, name, data, mode):
    with (directory_path / name).open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def save_journal(operation, record, api):
    api.atomic_write_private_json(operation / "journal.json", record)
    with directory(operation) as fd:
        os.fsync(fd)


def private_directory(path):
    with directory(path) as fd:
        item = os.fstat(fd)
        require(item.st_uid == os.geteuid() and stat.S_IMODE(item.st_mode) == 0o700,
                "local replay journal must be an owner-private directory")


def journal_record(operation):
    private_directory(operation)
    with directory(operation) as fd:
        data, mode = file_image(fd, "journal.json")
    require(mode == 0o600, "local replay journal has unexpected permissions")
    try:
        return json.loads(data)
    except ValueError as exc:
        raise LocalReplayError("invalid local replay journal") from exc


def prepare(root, store, plan, manifest, reason, api):
    operation = store / plan["planDigest"]
    require(not os.path.lexists(operation), "local replay plan already has a receipt; use --recover")
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=store) as name:
        stage = Path(name)
        product, final = prepared_objects(root, stage, plan, manifest, api)
        final_entries = tree(root, final)
        slots = {}
        for number, (path, before) in enumerate(plan["witness"]["checkout"].items()):
            slot = f"file-{number}"
            new = git(root, "cat-file", "blob", final_entries[path][1])
            create_file(stage, slot, new, before["mode"])
            slots[path] = {"slot": slot, "before": before,
                           "after": {"sha256": digest(new), "mode": before["mode"]}}
        for number, (path, group) in enumerate(plan["witness"].get("additions", {}).items()):
            slot = f"addition-{number}"
            expected = expected_addition(path, group, final_entries, root)
            if group["kind"] == "file":
                create_file(stage, slot, git(root, "cat-file", "blob", final_entries[path][1]), expected["mode"])
            else:
                subtree = stage / slot
                subtree.mkdir(mode=0o755)
                subtree.chmod(0o755)
                for relative in sorted(expected["directories"], key=lambda p: (p.count("/"), p)):
                    if relative:
                        target = subtree / relative
                        target.mkdir(mode=0o755)
                        target.chmod(0o755)
                for relative, wanted_image in expected["entries"].items():
                    target = subtree / relative
                    source = path + "/" + relative
                    create_file(target.parent, target.name,
                                git(root, "cat-file", "blob", final_entries[source][1]), wanted_image["mode"])
                for relative in sorted(expected["directories"], reverse=True):
                    with directory(subtree / relative) as fd:
                        os.fsync(fd)
            with directory(stage) as fd:
                require(added_image(fd, slot, group["kind"]) == expected,
                        "local replay staged addition differs from source objects")
                identity = added_identity(fd, slot, group["kind"])
            slots[path] = {"slot": slot, "kind": group["kind"], "before": None,
                           "after": expected, "identity": identity}
        create_file(stage, "index-lease", b"", 0o600)
        (stage / "prepared-index").chmod(plan["index"]["mode"])
        with (stage / "prepared-index").open("rb") as stream, directory(stage) as stage_fd:
            os.fsync(stream.fileno())
            index_after = image(stage_fd, "prepared-index")
        record = {
            "schemaVersion": 1, "state": "prepared", "plan": plan,
            "product": product, "final": final, "slots": slots,
            "indexAfter": index_after, "reason": reason,
        }
        save_journal(stage, record, api)
        os.rename(stage, operation)
        with directory(store) as fd:
            os.fsync(fd)
    return operation, record


def same_inode(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def owned_index_lock(meta, journal, *, acquire):
    seed = os.stat("index-lease", dir_fd=journal, follow_symlinks=False)
    require(stat.S_ISREG(seed.st_mode) and seed.st_uid == os.geteuid() and seed.st_size == 0,
            "local replay index lease seed is invalid")
    try:
        lock = os.stat("index.lock", dir_fd=meta, follow_symlinks=False)
    except FileNotFoundError:
        if not acquire:
            return False
        os.link("index-lease", "index.lock", src_dir_fd=journal, dst_dir_fd=meta,
                follow_symlinks=False)
        os.fsync(meta)
        lock = os.stat("index.lock", dir_fd=meta, follow_symlinks=False)
    require(same_inode(seed, lock), "local replay index is locked by another writer; lock preserved")
    return True


@contextlib.contextmanager
def operation_lock(git_dir, store_name="syncwheel-local-replay"):
    require(store_name in {"syncwheel-local-replay", "syncwheel-local-source", "syncwheel-local-declarations"}, "unsupported local operation store")
    store = git_dir / store_name
    store.mkdir(mode=0o700, exist_ok=True)
    with directory(git_dir) as meta:
        os.fsync(meta)
    private_directory(store)
    with directory(store) as fd, os.fdopen(os.open(
        "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd
    ), "r+b") as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600,
                "local replay operation lock has unexpected ownership")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LocalReplayError("another local replay writer is active") from exc
        yield store


def exchange(first, first_name, second, second_name):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    require(rename is not None, "local replay requires atomic Linux file exchange")
    if rename(first, os.fsencode(first_name), second, os.fsencode(second_name), 2):
        error = ctypes.get_errno()
        raise LocalReplayError(f"local replay atomic file exchange failed: {os.strerror(error)}")
    os.fsync(first)
    os.fsync(second)


def move_absent(first, first_name, second, second_name):
    """Atomic no-replace move; a concurrent untracked destination is never overwritten."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    require(rename is not None, "local replay requires atomic Linux no-replace rename")
    if rename(first, os.fsencode(first_name), second, os.fsencode(second_name), 1):
        error = ctypes.get_errno()
        raise LocalReplayError(f"local replay no-replace rename failed: {os.strerror(error)}")
    os.fsync(first)
    os.fsync(second)


def switch_addition(parent, name, journal, spec, *, forward, replace):
    current = added_image(parent, name, spec["kind"])
    retained = added_image(journal, spec["slot"], spec["kind"])
    want, previous = (spec["after"], None) if forward else (None, spec["after"])
    location = (parent, name) if current is not None else (journal, spec["slot"])
    require(added_identity(*location, spec["kind"]) == spec["identity"],
            "local replay addition inode changed or native identity changed; preserving the journal and checkout data")
    if (current, retained) == (want, previous):
        return
    require(replace and (current, retained) == (previous, want),
            "local replay addition or staged bytes changed; preserving the journal")
    if forward:
        move_absent(journal, spec["slot"], parent, name)
    else:
        move_absent(parent, name, journal, spec["slot"])
    destination = (parent, name) if forward else (journal, spec["slot"])
    require(added_identity(*destination, spec["kind"]) == spec["identity"],
            "local replay addition inode changed or native identity changed during adoption; journal retained")
    require((added_image(parent, name, spec["kind"]), added_image(journal, spec["slot"], spec["kind"]))
            == (want, previous), "local replay addition changed during adoption; journal retained")


def switch_image(parent, name, journal, slot, before, after, *, forward, replace=True):
    current, retained = image(parent, name), image(journal, slot)
    want, previous = (after, before) if forward else (before, after)
    if (current, retained) == (want, previous):
        return
    require(replace and (current, retained) == (previous, want),
            "local replay checkout or staged bytes changed; preserving the journal")
    exchange(parent, name, journal, slot)
    require((image(parent, name), image(journal, slot)) == (want, previous),
            "local replay file changed during exchange; both images retained")


class RefLease:
    """Hold native branch/HEAD locks across checkout adoption."""

    def __init__(self, root, git_dir, plan, current, *, new=None, source=False, verification_auth=False):
        self.root, self.git_dir, self.plan, self.current = root, git_dir, plan, current
        self.new, self.source = new, source
        self.verification_auth = verification_auth
        self.process = None

    def locked(self):
        refs = [self.plan["targetRef"], "HEAD"]
        if self.source:
            refs.append(self.plan["sourceRef"])
        return [self.git_dir / (ref + ".lock") for ref in refs]

    def send(self, words, expected):
        self.process.stdin.write(words + "\n")
        self.process.stdin.flush()
        require(self.process.stdout.readline().strip() == expected,
                "local replay ref transaction refused or lost its CAS lease")

    def check(self):
        p = self.plan
        require(symbolic(self.root, "HEAD") == p["targetRef"] and head(self.root, p["targetRef"]) == self.current
                and (not self.source or head(self.root, p["sourceRef"]) == p["sourceTip"]),
                "local replay HEAD or source drifted while acquiring ref leases")
        require(all(path.is_file() and not path.is_symlink() for path in self.locked()),
                "Git did not hold the expected HEAD and branch leases")

    @contextlib.contextmanager
    def hold(self, api):
        require(not any(os.path.lexists(path) for path in [*self.locked(), self.git_dir / "packed-refs.lock"]),
                "local replay found an unowned Git ref lock; no locks were removed")
        auth = None
        if (self.new or self.verification_auth) and self.plan["referenceHook"]:
            guard = api.require_primary_guard(self.root)
            require(guard["enabled"] and "refs/heads/" + guard["integrationBranch"] == self.plan["targetRef"],
                    "local replay managed reference hook does not protect the target")
            auth = api.authorize_ref_move(self.root)
        env = {api.MANAGED_REF_MOVE_AUTH_ENV: auth} if auth else None
        try:
            with subprocess.Popen(
                ["git", "-c", "core.fsync=loose-object,index,reference", "update-ref", "--stdin"],
                cwd=self.root, env=git_env(env), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            ) as self.process:
                try:
                    self.send("start", "start: ok")
                    target = self.plan["targetRef"]
                    operation = (f"update {target} {self.new} {self.current}" if self.new
                                 else f"verify {target} {self.current}")
                    commands = ["option no-deref", operation]
                    if self.source:
                        commands.extend(["option no-deref", f"verify {self.plan['sourceRef']} {self.plan['sourceTip']}"])
                    self.send("\n".join([*commands, "prepare"]), "prepare: ok")
                    self.check()
                    require(hook_observation(self.root, api) == self.plan["referenceHook"],
                            "local replay reference hook changed")
                    if auth:
                        receipt = json.loads((api.ref_auth_dir(self.root) / auth).read_bytes())
                        require(receipt.get("remaining") == ["pre-commit"],
                                "local replay could not prove execution of the managed reference hook")
                    yield self
                finally:
                    if self.process.poll() is None and not self.process.stdin.closed:
                        # EOF discards a prepared transaction that was not committed.
                        self.process.stdin.close()
        finally:
            if auth:
                (api.ref_auth_dir(self.root) / auth).unlink(missing_ok=True)

    def commit(self):
        self.check()
        self.send("commit", "commit: ok")
        self.process.stdin.close()
        require(self.process.wait() == 0, "local replay ref commit outcome is uncertain")


def aligned_files(work, meta, journal, record, *, forward, replace=True):
    for path, spec in record["slots"].items():
        with parent_dir(work, path) as (parent, name):
            if spec["before"] is None:
                switch_addition(parent, name, journal, spec, forward=forward, replace=replace)
                checkpoint("checkout-addition-aligned")
            else:
                switch_image(parent, name, journal, spec["slot"], spec["before"], spec["after"],
                             forward=forward, replace=replace)
        checkpoint("checkout-file-aligned")
    switch_image(meta, "index", journal, "prepared-index", record["plan"]["index"],
                 record["indexAfter"], forward=forward, replace=replace)
    checkpoint("index-aligned")


def verify_objects(root, record, api):
    plan = record["plan"]
    old_tree, expected = tree(root, plan["integrationTip"]), tree(root, record["final"])
    product = tree(root, record["product"])
    witness = plan["witness"]["paths"]
    require(git(root, "rev-list", "--parents", "-n", "1", record["product"]).decode().split()
            == [record["product"], plan["integrationTip"]]
            and git(root, "rev-list", "--parents", "-n", "1", record["final"]).decode().split()
            == [record["final"], record["product"]]
            and all(old_tree.get(path) == spec["before"] for path, spec in witness.items())
            and product == {**old_tree, **{path: spec["after"] for path, spec in witness.items()}}
            and expected == {**product, MANIFEST: expected[MANIFEST]}
            and expected[MANIFEST] != product[MANIFEST],
            "local replay journal objects do not match the product witness")
    original = api.normalize_manifest_data(
        json.loads(git(root, "cat-file", "blob", old_tree[MANIFEST][1])), root / MANIFEST,
    )
    observed = json.loads(git(root, "cat-file", "blob", expected[MANIFEST][1]))
    selected = api.require_stack(original, plan["stack"])
    selected["integration_commits"] = [record["product"]]
    selected["integration_resolution"] = {"source_tip": plan["sourceTip"]}
    # load_manifest adds normalized defaults; compare the prepared canonical manifest
    # against the manifest reconstructed from the immutable preimage below.
    require(api.canonical_manifest_file_text(original) == api.canonical_manifest_file_text(observed),
            "local replay journal control object changes more than selected ownership")
    observed_paths = plan["witness"]["checkout"]
    additions = addition_roots(old_tree, witness)
    require(plan["witness"].get("additions", {}) == additions,
            "local replay addition roots differ from the source witness")
    existing = {path for path in witness if path in old_tree} | {MANIFEST}
    require(record["slots"].keys() == existing | additions.keys() and observed_paths.keys() == existing,
            "local replay journal slots differ from the witness")
    for number, (path, before) in enumerate(sorted(observed_paths.items())):
        slot = record["slots"][path]
        contents = git(root, "cat-file", "blob", expected[path][1])
        require(slot == {"slot": f"file-{number}", "before": before,
                         "after": {"sha256": digest(contents), "mode": before["mode"]}},
                "local replay journal has an invalid checkout slot")
    for number, (path, group) in enumerate(additions.items()):
        slot = record["slots"][path]
        identity = slot.get("identity", {})
        identity_keys = {"device", "inode", "fileHandle"} | ({"descendants"} if group["kind"] == "directory" else set())
        require(set(identity) == identity_keys
                and all(type(identity.get(key)) is int and identity[key] >= 0 for key in ("device", "inode"))
                and identity["inode"] > 0 and valid_native_handle(identity.get("fileHandle")),
                "local replay addition lacks a valid inode witness")
        expected_image = expected_addition(path, group, expected, root)
        if group["kind"] == "directory":
            descendants = identity["descendants"]
            required_kinds = {key: "file" for key in expected_image["entries"]}
            required_kinds.update((key, "directory") for key in expected_image["directories"] if key)
            require(type(descendants) is dict and descendants.keys() == required_kinds.keys(),
                    "local replay addition lacks exact descendant inode witnesses")
            for relative, child_identity in descendants.items():
                require(type(child_identity) is dict and set(child_identity) == {"device", "inode", "kind", "fileHandle"}
                        and type(child_identity["device"]) is int and child_identity["device"] == identity["device"]
                        and type(child_identity["inode"]) is int and child_identity["inode"] > 0
                        and child_identity["kind"] == required_kinds[relative]
                        and valid_native_handle(child_identity["fileHandle"]),
                        "local replay addition has an invalid descendant inode witness")
        require(slot == {"slot": f"addition-{number}", "kind": group["kind"], "before": None,
                         "after": expected_image, "identity": identity},
                "local replay journal has an invalid addition slot")
    return expected


def finish(root, git_dir, operation, record, api, *, recover=False):
    plan = record["plan"]
    with directory(root) as work, directory(git_dir) as meta, directory(operation) as journal:
        # The durable aligning marker is written before any checkout/index
        # exchange. A prepared-only intent can be closed without reading the
        # current branch: release only its exact seed-owned index lock.
        if recover and (record["state"] == "prepared" or record.get("abortedBeforeAlignment")):
            paths = RefLease(root, git_dir, plan, None, source=True).locked()
            require(not any(os.path.lexists(path) for path in [*paths, git_dir / "packed-refs.lock"]),
                    "local replay found an unowned Git ref lock; no locks were removed")
            release = owned_index_lock(meta, journal, acquire=False)
            if record["state"] == "prepared":
                record.update(state="aborted", abortedBeforeAlignment=True)
                save_journal(operation, record, api)
                checkpoint("terminal-recorded")
            if release and owned_index_lock(meta, journal, acquire=False):
                os.unlink("index.lock", dir_fd=meta)
                os.fsync(meta)
            return record["state"]
        current = head(root, plan["targetRef"])
        require(current in {plan["integrationTip"], record["final"]}, "local replay target advanced; journal retained")
        require(symbolic(root, "HEAD") == plan["targetRef"], "local replay checkout changed; journal retained")
        require(hook_observation(root, api) == plan["referenceHook"], "local replay reference hook changed")
        if record["state"] in {"completed", "aborted"}:
            require(recover, "local replay plan already has a terminal receipt")
            expected = record["final"] if record["state"] == "completed" else plan["integrationTip"]
            require(current == expected, "local replay target differs from its terminal receipt")
            if owned_index_lock(meta, journal, acquire=False):
                with RefLease(root, git_dir, plan, current).hold(api):
                    os.unlink("index.lock", dir_fd=meta)
                    os.fsync(meta)
            return record["state"]
        require(record["state"] == ("aligning" if recover else "prepared"),
                "invalid local replay journal state")
        expected_final = verify_objects(root, record, api)
        owned_index_lock(meta, journal, acquire=True)
        checkpoint("index-lock-owned")
        if not recover:
            require(current == plan["integrationTip"], "local replay target changed before adoption")
        forward = not recover or current == record["final"]
        lease = RefLease(root, git_dir, plan, current, new=None if recover else record["final"], source=not recover)
        with lease.hold(api):
            checkpoint("ref-transaction-prepared")
            if not recover:
                refreshed, _ = make_plan(root, plan["stack"], api)
                require(refreshed == plan, "local replay preview is stale under the ref and index leases")
                record["state"] = "aligning"
                save_journal(operation, record, api)
                checkpoint("alignment-authorized")
            aligned_files(work, meta, journal, record, forward=forward,
                          replace=not recover or not forward)
            expected = expected_final if forward else tree(root, current)
            require_no_attributes(root, expected)
            require_clean_index(root, expected)
            checkout(work, expected)
            require(owned_index_lock(meta, journal, acquire=False), "local replay lost the index lock")
            lease.check()
            if not recover:
                lease.commit()
                checkpoint("ref-committed")
            record["state"] = "completed" if forward else "aborted"
            save_journal(operation, record, api)
            checkpoint("terminal-recorded")
            require(owned_index_lock(meta, journal, acquire=False), "local replay lost the index lock")
            os.unlink("index.lock", dir_fd=meta)
            os.fsync(meta)
        return record["state"]


def execute_local_replay(root, args, api):
    git_dir = topology(root, api)
    if not args.apply and not args.recover:
        require(args.plan_digest is None and args.reason is None, "preview does not accept an apply digest or reason")
        return make_plan(root, args.stack, api)[0]
    require(args.plan_digest and re.fullmatch(r"[0-9a-f]{64}", args.plan_digest),
            "local replay requires an exact --plan-digest")
    require(not args.apply or (args.reason and args.reason.strip() and len(args.reason) <= 512),
            "local replay apply requires a bounded --reason")
    operation = git_dir / "syncwheel-local-replay" / args.plan_digest
    if args.recover:
        require(operation.is_dir(), "local replay has no journal for this digest")
    else:
        observed, _ = make_plan(root, args.stack, api)
        require(observed["planDigest"] == args.plan_digest, "local replay preview is stale")
    with operation_lock(git_dir) as store:
        with api.channel_mutation_lock(root, root / MANIFEST, "local-replay"):
            if args.recover:
                record = journal_record(operation)
                plan = record["plan"]
                unsigned = {key: value for key, value in plan.items() if key != "planDigest"}
                require(record["schemaVersion"] == 1 and plan["stack"] == args.stack
                        and plan["planDigest"] == args.plan_digest == api.canonical_json_digest(unsigned),
                        "local replay journal does not match the requested plan")
            else:
                for path in store.iterdir():
                    if re.fullmatch(r"[0-9a-f]{64}", path.name):
                        require(journal_record(path)["state"] in {"aborted", "completed"},
                                "a local replay journal is pending; recover that operation first")
                require(not api.pending_control_manifest_intents(api.load_ledger_events(root, root / MANIFEST)),
                        "a control manifest operation is pending; local replay will not recover it")
                plan, manifest = make_plan(root, args.stack, api)
                require(plan["planDigest"] == args.plan_digest, "local replay preview is stale")
                operation, record = prepare(root, store, plan, manifest, args.reason, api)
                checkpoint("journal-prepared")
            state = finish(root, git_dir, operation, record, api, recover=args.recover)
    after = record["final"] if state == "completed" else plan["integrationTip"]
    if record.get("abortedBeforeAlignment"):
        after = None  # No claim about a ref the operation never moved.
    return {"status": state, "stack": args.stack, "planDigest": args.plan_digest,
            "before": plan["integrationTip"], "after": after}


def local_source_plan(root, stack_id, api):
    """Observe an already declared linear source; never project or publish it."""
    meta = topology(root, api)
    manifest, _ = api.load_manifest(root, root / MANIFEST)
    require(manifest and manifest.get("syncwheel_tracking") == "git-tracked"
            and manifest.get("repository_mode", "delivery") == "delivery",
            "local materialization requires a tracked delivery manifest")
    authority = api.manifest_authority(manifest)
    require(authority["mode"] == api.AUTHORITY_MODE_AI_MANAGED
            and "source_change" in authority["allow"] and "source_change" not in authority["deny"],
            "local materialization requires source-change authority")
    stack = api.require_stack(manifest, stack_id)
    target = "refs/heads/" + stack["branch"]
    require(SAFE_REF.fullmatch(target) and target != "refs/heads/" + manifest["integration"]["branch"]
            and not symbolic(root, target), "local materialization requires a distinct direct source ref")
    git(root, "check-ref-format", target)
    require(not any(item.get("branch") == stack["branch"] for item in api.get_worktrees(root)),
            "local materialization refuses a checked-out target")
    registry, _ = api.load_governed_worktree_registry(root)
    require(not any(lane.get("state") != "reaped" and lane.get("branch") == stack["branch"]
                    for lane in registry["lanes"]), "local materialization refuses a governed lane ref")
    commits = stack["commits"]
    require(0 <= len(commits) <= 200 and all(type(pin) is str and OID.fullmatch(pin)
            for pin in [stack["base"], *commits]), "local materialization requires exact local commit IDs")
    previous = oid(root, stack["base"] + "^{commit}")
    for commit in commits:
        require(git(root, "rev-list", "--parents", "-n", "1", commit).decode().split() == [commit, previous],
                "local materialization requires an exact linear source sequence")
        previous = commit
    current_head = symbolic(root, "HEAD")
    require(current_head and SAFE_REF.fullmatch(current_head), "local materialization requires a direct branch checkout")
    current_tip = head(root, current_head)
    baseline = tree(root, current_tip)
    require(MANIFEST in baseline, "local materialization requires the tracked primary manifest")
    require_no_attributes(root, baseline)
    require_clean_index(root, baseline)
    with directory(root) as work, directory(meta) as metadata:
        observed = checkout(work, baseline)
        index = image(metadata, "index")
        with parent_dir(work, MANIFEST) as (parent, name):
            manifest_image = image(parent, name)
    existing = git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", target, allowed=(0, 1)).decode().strip() or None
    require(existing in {None, previous}, "local materialization preserves an existing different target")
    require(not api.pending_control_manifest_intents(api.load_ledger_events(root, root / MANIFEST)),
            "local materialization refuses a pending control operation")
    payload = {"schemaVersion": 1, "kind": "localSourceMaterialization", "stack": stack_id,
               "sourceRef": target, "sourceTip": previous, "sourceBase": stack["base"],
               "commits": commits, "expectedTargetTip": existing, "headRef": current_head,
               "headTip": current_tip, "manifest": manifest_image, "index": index,
               "checkout": observed, "referenceHook": hook_observation(root, api)}
    require(symbolic(root, "HEAD") == current_head and head(root, current_head) == current_tip,
            "local materialization HEAD drifted during planning")
    payload["planDigest"] = api.canonical_json_digest(payload)
    return payload


def create_local_source_ref(root, plan, api, env):
    """Prepare create-only CAS, then reject dangling aliases while the ref is locked."""
    with subprocess.Popen(
        ["git", "-c", "core.fsync=loose-object,index,reference", "update-ref", "--stdin"],
        cwd=root, env=git_env(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    ) as process:
        def send(command, expected):
            process.stdin.write(command + "\n")
            process.stdin.flush()
            require(process.stdout.readline().strip() == expected,
                    "local materialization create-only CAS refused or outcome uncertain")
        try:
            send("start", "start: ok")
            send("option no-deref\ncreate " + plan["sourceRef"] + " " + plan["sourceTip"]
                 + "\nverify HEAD " + plan["headTip"] + "\nprepare", "prepare: ok")
            # Old Git represents an unborn symbolic target as a zero OID. Check
            # its name under the prepared lock before committing, never dereference it.
            require(not symbolic(root, plan["sourceRef"])
                    and not os.path.lexists(root / ".git" / plan["sourceRef"]),
                    "local materialization concurrent ref name preserved")
            require(symbolic(root, "HEAD") == plan["headRef"]
                    and head(root, plan["headRef"]) == plan["headTip"]
                    and all((root / ".git" / (ref + ".lock")).is_file()
                            for ref in ("HEAD", plan["headRef"], plan["sourceRef"])),
                    "local materialization HEAD lease drifted")
            require(local_source_plan(root, plan["stack"], api) == plan,
                    "local materialization witness drifted under native leases")
            checkpoint("source-ref-prepared")
            send("commit", "commit: ok")
            process.stdin.close()
            require(process.wait() == 0, "local materialization ref commit outcome uncertain")
        finally:
            if process.poll() is None and not process.stdin.closed:
                process.stdin.close()  # EOF aborts an uncommitted prepared transaction.


def source_receipt(operation, stack_id, digest_value, api):
    record = journal_record(operation)
    require(type(record) is dict and type(record.get("plan")) is dict
            and record.get("state") in {"prepared", "held", "completed"}, "invalid local materialization receipt")
    plan = record["plan"]
    unsigned = {key: value for key, value in plan.items() if key != "planDigest"}
    require(record.get("schemaVersion") == 1 and record.get("kind") == "localSourceIntent"
            and plan.get("kind") == "localSourceMaterialization" and plan.get("stack") == stack_id
            and plan.get("planDigest") == digest_value == api.canonical_json_digest(unsigned),
            "local materialization receipt scope mismatch")
    return record


def recover_source_intent(root, args, api, operation):
    record = source_receipt(operation, args.stack, args.plan_digest, api)
    plan = record["plan"]
    # Never infer that an absent ref means the first attempt did not run. A
    # recovery only releases this seed-owned index lease; it never moves refs.
    require(not any(os.path.lexists(root / ".git" / (ref + ".lock"))
                    for ref in ("HEAD", plan["headRef"], plan["sourceRef"])),
            "local materialization native ref locks remain; locks preserved")
    released = False
    with directory(root / ".git") as meta, directory(operation) as journal:
        if owned_index_lock(meta, journal, acquire=False):
            os.unlink("index.lock", dir_fd=meta)
            os.fsync(meta)
            released = True
    observed = git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", plan["sourceRef"], allowed=(0, 1)).decode().strip() or None
    symbolic_target = symbolic(root, plan["sourceRef"])
    state = "completed" if record["state"] == "completed" and observed == plan["sourceTip"] and not symbolic_target else "held"
    record["state"] = state
    save_journal(operation, record, api)
    return {"status": state, "sourceRef": plan["sourceRef"], "observedTip": observed,
            "observedSymbolicTarget": symbolic_target or None, "planDigest": args.plan_digest,
            "retryAllowed": False, "indexLeaseReleased": released}


def execute_source_materialization(root, args, api):
    if args.recover:
        require(type(args.plan_digest) is str and HASH64.fullmatch(args.plan_digest) and args.reason is None,
                "local materialization recovery requires only its exact digest")
        meta = topology(root, api)
        manifest, _ = api.load_manifest(root, root / MANIFEST)
        authority = api.manifest_authority(manifest)
        require(authority["mode"] == api.AUTHORITY_MODE_AI_MANAGED and "source_change" in authority["allow"]
                and "source_change" not in authority["deny"], "local materialization recovery requires source-change authority")
        operation = meta / "syncwheel-local-source" / args.plan_digest
        require(operation.is_dir(), "local materialization has no receipt for this digest")
        with operation_lock(meta, "syncwheel-local-source"):
            with api.channel_mutation_lock(root, root / MANIFEST, "local-source-recovery"):
                return recover_source_intent(root, args, api, operation)
    plan = local_source_plan(root, args.stack, api)
    if not args.apply:
        require(args.plan_digest is None and args.reason is None,
                "local materialization preview does not accept an apply digest or reason")
        return plan
    require(type(args.plan_digest) is str and args.plan_digest == plan["planDigest"]
            and type(args.reason) is str and 0 < len(args.reason.strip()) <= 512,
            "local materialization requires the current digest and bounded reason")
    with operation_lock(root / ".git", "syncwheel-local-source") as store:
        with api.channel_mutation_lock(root, root / MANIFEST, "local-source-materialization"):
            require(local_source_plan(root, args.stack, api) == plan, "local materialization plan drifted")
            prior = []
            for path in store.iterdir():
                if HASH64.fullmatch(path.name):
                    record = journal_record(path)
                    require(type(record) is dict and type(record.get("plan")) is dict, "invalid local materialization receipt")
                    record = source_receipt(path, record["plan"].get("stack"), path.name, api)
                    if record["plan"].get("sourceRef") == plan["sourceRef"]:
                        prior.append(record)
            if plan["expectedTargetTip"] is not None:
                require(not any(record["state"] != "completed" for record in prior),
                        "local materialization intent requires explicit recovery; no retry")
                return {"status": "present_exact", "created": False, "ownership": "not_inferred",
                        "sourceRef": plan["sourceRef"], "sourceTip": plan["sourceTip"], "planDigest": plan["planDigest"]}
            require(not prior, "local materialization already has a source intent; no automatic re-creation")
            operation = store / plan["planDigest"]
            operation.mkdir(mode=0o700)
            create_file(operation, "index-lease", b"", 0o600)
            record = {"schemaVersion": 1, "kind": "localSourceIntent", "state": "prepared", "plan": plan, "reason": args.reason}
            save_journal(operation, record, api)
            with directory(store) as descriptor:
                os.fsync(descriptor)
            api.append_ledger_event(root, "local_source_materialization_intent", {
                "planDigest": plan["planDigest"], "sourceRef": plan["sourceRef"], "sourceTip": plan["sourceTip"],
                "expectedTargetTip": None, "reason": args.reason, "manifestWitness": plan["manifest"],
            }, root / MANIFEST)
            checkpoint("source-intent-prepared")
            with directory(root / ".git") as metadata, directory(operation) as journal:
                owned_index_lock(metadata, journal, acquire=True)
                token = None
                try:
                    checkpoint("source-before-create")
                    require(local_source_plan(root, args.stack, api) == plan, "local materialization plan drifted before creation")
                    token = api.authorize_ref_move(root) if plan["referenceHook"] else None
                    checkpoint("source-create-cas")
                    create_local_source_ref(root, plan, api, {api.MANAGED_REF_MOVE_AUTH_ENV: token} if token else None)
                    checkpoint("source-created")
                    require(head(root, plan["sourceRef"]) == plan["sourceTip"], "local materialization target drifted; current ref preserved")
                    record["state"] = "completed"
                    save_journal(operation, record, api)
                    api.append_ledger_event(root, "local_source_materialization_completed", {
                        "planDigest": plan["planDigest"], "sourceRef": plan["sourceRef"], "sourceTip": plan["sourceTip"],
                    }, root / MANIFEST)
                finally:
                    if token:
                        (api.ref_auth_dir(root) / token).unlink(missing_ok=True)
                    if owned_index_lock(metadata, journal, acquire=False):
                        os.unlink("index.lock", dir_fd=metadata)
                        os.fsync(metadata)
    return {"status": "created", "created": True, "sourceRef": plan["sourceRef"],
            "sourceTip": plan["sourceTip"], "planDigest": plan["planDigest"]}


def declaration_observation(root, api):
    """Observe a clean primary without recovering any other operation."""
    meta = topology(root, api)
    manifest, _ = api.load_manifest(root, root / MANIFEST)
    require(manifest and manifest.get("syncwheel_tracking") == "git-tracked"
            and manifest.get("repository_mode", "delivery") == "delivery",
            "local declaration requires the primary tracked delivery manifest")
    authority = api.manifest_authority(manifest)
    require(authority["mode"] == api.AUTHORITY_MODE_AI_MANAGED
            and "source_change" in authority["allow"] and "source_change" not in authority["deny"],
            "local declaration requires source-change authority")
    target = "refs/heads/" + manifest["integration"]["branch"]
    require(SAFE_REF.fullmatch(target) and symbolic(root, "HEAD") == target,
            "local declaration primary changed before its write lease")
    if api.primary_guard_path(root).exists():
        guard = api.require_primary_guard(root)
        require(not guard["enabled"] or target == "refs/heads/" + guard["integrationBranch"],
                "local declaration guard and primary differ")
    tip = head(root, target)
    expected = tree(root, tip)
    require(MANIFEST in expected, "local declaration requires a tracked manifest")
    require_no_attributes(root, expected)
    require_clean_index(root, expected)
    with directory(root) as work, directory(meta) as metadata:
        observed = checkout(work, expected)
        index = image(metadata, "index")
    require(symbolic(root, "HEAD") == target and head(root, target) == tip,
            "local declaration HEAD drifted while observing")
    return {"targetRef": target, "integrationTip": tip, "index": index,
            "checkout": observed, "referenceHook": hook_observation(root, api)}, manifest


def verify_declaration_record(root, operation, api):
    record = journal_record(operation)
    require(record.get("kind") == "localManifestDeclaration"
            and record.get("state") in {"prepared", "aligning", "completed", "aborted"},
            "invalid local declaration journal")
    plan = record["plan"]
    require(plan["planDigest"] == operation.name
            and api.canonical_json_digest({k: v for k, v in plan.items() if k != "planDigest"}) == operation.name,
            "local declaration journal digest differs")
    before = tree(root, plan["integrationTip"])
    after = tree(root, record["final"])
    require(git(root, "rev-list", "--parents", "-n", "1", record["final"]).decode().split()
            == [record["final"], plan["integrationTip"]]
            and after == {**before, MANIFEST: after[MANIFEST]},
            "local declaration journal changes more than the control manifest")
    encoded = api.canonical_manifest_file_text(plan["manifestAfter"]).encode()
    require(after[MANIFEST] == ["100644", blob_oid(encoded)]
            and record["slots"] == {MANIFEST: {"slot": "manifest", "before": plan["checkout"][MANIFEST],
                    "after": {"sha256": digest(encoded), "mode": plan["checkout"][MANIFEST]["mode"]}}},
            "local declaration manifest slot differs from its object")
    return record


def finish_declaration(root, operation, record, api, *, recover=False):
    plan = record["plan"]
    meta = topology(root, api)
    current = head(root, plan["targetRef"])
    require(symbolic(root, "HEAD") == plan["targetRef"]
            and current in {plan["integrationTip"], record["final"]},
            "local declaration primary drifted; journal and checkout preserved")
    require(hook_observation(root, api) == plan["referenceHook"], "local declaration reference hook changed")
    verify_declaration_record(root, operation, api)
    with directory(root) as work, directory(meta) as metadata, directory(operation) as journal:
        if record["state"] in {"completed", "aborted"}:
            require(recover, "local declaration already has a terminal receipt")
            expected_tip = record["final"] if record["state"] == "completed" else plan["integrationTip"]
            require(current == expected_tip, "local declaration terminal ref drifted")
            require_clean_index(root, tree(root, current)); checkout(work, tree(root, current))
            # A terminal-record crash may leave our own index lease. Native
            # ref locks still have no recoverable ownership proof.
            with RefLease(root, meta, plan, current, verification_auth=True).hold(api):
                if owned_index_lock(metadata, journal, acquire=False):
                    os.unlink("index.lock", dir_fd=metadata); os.fsync(metadata)
            return record["state"]
        owned_index_lock(metadata, journal, acquire=True)
        checkpoint("declaration-index-owned")
        forward = not recover or current == record["final"]
        lease = RefLease(root, meta, plan, current, new=None if recover else record["final"], verification_auth=True)
        try:
            with lease.hold(api):
                checkpoint("declaration-ref-prepared")
                if not recover:
                    observed, _ = declaration_observation(root, api)
                    require(observed == {k: plan[k] for k in observed},
                            "local declaration witness drifted under its leases")
                    record["state"] = "aligning"
                    save_journal(operation, record, api)
                aligned_files(work, metadata, journal, record, forward=forward)
                expected = tree(root, record["final"] if forward else plan["integrationTip"])
                require_no_attributes(root, expected)
                require_clean_index(root, expected); checkout(work, expected)
                lease.check()
                if not recover:
                    lease.commit()
                    checkpoint("declaration-ref-committed")
                record["state"] = "completed" if forward else "aborted"
                save_journal(operation, record, api)
                checkpoint("declaration-terminal-recorded")
                require(owned_index_lock(metadata, journal, acquire=False), "local declaration lost its index lease")
                os.unlink("index.lock", dir_fd=metadata); os.fsync(metadata)
        except Exception:
            # No alignment was authorized: release only the exact seed-owned
            # index lease, without restoring any checkout/index/ref bytes.
            if record["state"] == "prepared":
                record["state"] = "aborted"
                save_journal(operation, record, api)
                if owned_index_lock(metadata, journal, acquire=False):
                    os.unlink("index.lock", dir_fd=metadata); os.fsync(metadata)
            raise
    return record["state"]


def append_declaration_receipt(root, operation, record, api):
    """Normal completion and recovery attest one identical durable operation."""
    plan = record["plan"]
    receipt = {"status": "completed", "planDigest": operation.name, "controlCommit": record["final"]}
    api.append_ledger_event(root, "manifest_saved", api.manifest_event_payload(
        root / MANIFEST, plan["manifestAfter"], "stack_create", {
            "stack": plan["stack"], "branch": api.require_stack(plan["manifestAfter"], plan["stack"])["branch"],
            "actor": record.get("actor", "unknown"), "declaration_receipt": receipt,
        }), root / MANIFEST, idempotency_key="local-declaration-manifest:" + operation.name)
    return receipt


def apply_manifest_declaration(root, updated, stack_id, api):
    with sanitized_cli_environment():
        observation, original = declaration_observation(root, api)
        proposed = copy.deepcopy(updated)
        proposed["stacks"] = [s for s in proposed["stacks"] if s["id"] != stack_id]
        proposed["integration"]["stacks"] = [s for s in proposed["integration"]["stacks"] if s != stack_id]
        require(proposed == original and stack_id not in api.stack_map(original),
                "local declaration may only add its selected stack and membership")
        plan = {**observation, "manifestAfter": updated, "stack": stack_id}
        plan["planDigest"] = api.canonical_json_digest(plan)
        with operation_lock(root / ".git", "syncwheel-local-declarations") as store:
            require(not any(journal_record(p).get("state") not in {"completed", "aborted"}
                            for p in store.iterdir() if HASH64.fullmatch(p.name)),
                    "local declaration has an interrupted intent; use stack recover-declaration")
            operation = store / plan["planDigest"]
            require(not operation.exists(), "local declaration already has a receipt; inspect it explicitly")
            with tempfile.TemporaryDirectory(prefix=".prepare-", dir=store) as temporary:
                stage = Path(temporary)
                final = api.materialize_control_manifest_commit(root, updated, plan["integrationTip"])
                encoded = api.canonical_manifest_file_text(updated).encode()
                create_file(stage, "manifest", encoded, plan["checkout"][MANIFEST]["mode"])
                create_file(stage, "index-lease", b"", 0o600)
                git(root, "read-tree", final, env={"GIT_INDEX_FILE": str(stage / "prepared-index")})
                (stage / "prepared-index").chmod(plan["index"]["mode"])
                with directory(stage) as fd:
                    with (stage / "prepared-index").open("rb") as stream: os.fsync(stream.fileno())
                    index_after = image(fd, "prepared-index")
                record = {"kind": "localManifestDeclaration", "schemaVersion": 1, "state": "prepared",
                          "actor": api.governed_worktree_owner(),
                          "plan": plan, "final": final, "indexAfter": index_after,
                          "slots": {MANIFEST: {"slot": "manifest", "before": plan["checkout"][MANIFEST],
                                               "after": {"sha256": digest(encoded), "mode": plan["checkout"][MANIFEST]["mode"]}}}}
                save_journal(stage, record, api)
                os.rename(stage, operation)
                with directory(store) as fd: os.fsync(fd)
            api.append_ledger_event(root, "local_declaration_intent", {
                "planDigest": plan["planDigest"], "stack": stack_id, "oldTip": plan["integrationTip"], "newTip": final,
            }, root / MANIFEST)
            checkpoint("declaration-before-leases")
            status = finish_declaration(root, operation, record, api)
            require(status == "completed", "local declaration did not complete")
            transaction = api.active_manifest_write_transaction(root / MANIFEST)
            if transaction is not None:
                transaction["expectedDigest"] = api.manifest_digest(updated)
            receipt = append_declaration_receipt(root, operation, record, api)
            checkpoint("declaration-receipt-appended")
            return receipt


def recover_manifest_declaration(root, args, api):
    meta = topology(root, api)
    require(type(args.plan_digest) is str and HASH64.fullmatch(args.plan_digest), "recovery requires an exact declaration digest")
    operation = meta / "syncwheel-local-declarations" / args.plan_digest
    record = verify_declaration_record(root, operation, api)
    if not args.apply:
        return {"status": record["state"], "planDigest": args.plan_digest, "applyRequired": True}
    require(type(args.reason) is str and 0 < len(args.reason.strip()) <= 512, "declaration recovery requires a bounded reason")
    manifest, _ = api.load_manifest(root, root / MANIFEST)
    authority = api.manifest_authority(manifest)
    require(authority["mode"] == api.AUTHORITY_MODE_AI_MANAGED and "source_change" in authority["allow"]
            and "source_change" not in authority["deny"], "declaration recovery requires source-change authority")
    with operation_lock(meta, "syncwheel-local-declarations"):
        with api.channel_mutation_lock(root, root / MANIFEST, "local-declaration-recovery"):
            record = verify_declaration_record(root, operation, api)
            if "recoveryReason" not in record:
                record["recoveryReason"] = args.reason
                save_journal(operation, record, api)
            status = finish_declaration(root, operation, record, api, recover=True)
            api.append_ledger_event(root, "local_declaration_recovered", {
                "planDigest": args.plan_digest, "stack": record["plan"]["stack"],
                "status": status, "reason": record["recoveryReason"],
            }, root / MANIFEST, idempotency_key="local-declaration-recovered:" + args.plan_digest + ":" + status)
            if status == "completed":
                append_declaration_receipt(root, operation, record, api)
            return {"status": status, "planDigest": args.plan_digest, "retryAllowed": False}
