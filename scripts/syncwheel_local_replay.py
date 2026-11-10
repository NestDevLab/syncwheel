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
SAFE_REF = re.compile(r"refs/heads/[A-Za-z0-9._/-]+")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
MAX_ENTRIES = 10000
MAX_BYTES = 32 * 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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
    require(git_dir.is_dir() and not git_dir.is_symlink()
            and git(root, "rev-parse", "--show-toplevel").decode().strip() == str(root)
            and git(root, "rev-parse", "--show-object-format").decode().strip() == "sha1"
            and git(root, "rev-parse", "--is-shallow-repository").decode().strip() == "false"
            and not git(root, "config", "--get-regexp",
                        r"^(extensions\.(partialclone|refstorage)|remote\..*\.promisor|core\.(splitindex|sparsecheckout|worktree|fsmonitor|autocrlf|eol|attributesfile)|index\.sparse|filter\..+)$",
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
    require(original.keys() == wanted.keys(), "local replay refuses source additions and deletions")
    changes = {path: {"before": original[path], "after": wanted[path]} for path in wanted
               if original[path] != wanted[path]}
    require(changes and not any(path.startswith(".syncwheel/") or path in {".gitignore", ".gitattributes"}
                                for path in changes),
            "local replay requires a nonempty product-only source delta")
    for path, entry in changes.items():
        require(entry["before"][0] == entry["after"][0] and baseline.get(path) == entry["before"],
                f"local replay conflict or unsupported mode change at {path}")
    require(baseline.get(MANIFEST, [None])[0] == "100644",
            "local replay requires a tracked control manifest")
    require_no_attributes(root, baseline)
    require_clean_index(root, baseline)
    with directory(root) as work, directory(git_dir) as meta:
        # The complete tracked checkout is verified without filters or optional index updates.
        verified = checkout(work, baseline)
        observed = {path: verified[path] for path in sorted({*changes, MANIFEST})}
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
        "witness": {"method": "exact-path-preimages", "paths": changes, "checkout": observed},
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
        git(root, "update-index", "--cacheinfo", f"{mode},{blob},{path}", env=env)
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
def operation_lock(git_dir):
    store = git_dir / "syncwheel-local-replay"
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

    def __init__(self, root, git_dir, plan, current, *, new=None, source=False):
        self.root, self.git_dir, self.plan, self.current = root, git_dir, plan, current
        self.new, self.source = new, source
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
        if self.new and self.plan["referenceHook"]:
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
    require(record["slots"].keys() == observed_paths.keys() == {*witness, MANIFEST},
            "local replay journal slots differ from the witness")
    for number, (path, before) in enumerate(sorted(observed_paths.items())):
        slot = record["slots"][path]
        contents = git(root, "cat-file", "blob", expected[path][1])
        require(slot == {"slot": f"file-{number}", "before": before,
                         "after": {"sha256": digest(contents), "mode": before["mode"]}},
                "local replay journal has an invalid checkout slot")
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


def execute(root, args, api):
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
