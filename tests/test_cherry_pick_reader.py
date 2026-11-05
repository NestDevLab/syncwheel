"""Generic native-object fixtures for the reader; no real recovery is exercised."""
import base64
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))
import syncwheel_cherry_pick_preview as pure
import syncwheel_cherry_pick_reader as reader
from test_cherry_pick_preview import Fixture


class Repository:
    def __init__(self, count=2):
        self.directory = tempfile.TemporaryDirectory(prefix="reader-object-fixture-")
        self.root = Path(self.directory.name)
        self.fixture = Fixture(count=count)
        self.git("init", "--quiet")
        self.emit()
        self.git("update-ref", "refs/heads/base", self.fixture.frozen["base"])
        self.git("update-ref", "refs/heads/backup", self.fixture.frozen["backup"])
        for branch, oid in self.fixture.frozen["sourcePins"].items():
            self.git("update-ref", "refs/heads/" + branch, oid)
        self.git("symbolic-ref", "HEAD", "refs/heads/base")
        self.git("read-tree", self.fixture.frozen["base"])
        (self.root / "untouched.txt").write_bytes(b"worktree sentinel\n")

    def git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.root), *args], input=data,
                              capture_output=True, check=True).stdout

    def emit(self):
        for oid, item in self.fixture.objects.items():
            result = self.git("hash-object", "-w", "-t", item["type"], "--stdin",
                              data=base64.b64decode(item["data"]))
            if result.decode().strip() != oid:
                raise AssertionError("fixture object identity mismatch")

    def replace_manifest(self, manifest):
        f = self.fixture
        raw = json.dumps(manifest).encode() + b"\n"
        oid = f.commit({".syncwheel/manifest.json": raw}, f.frozen["base"])
        self.emit()
        self.git("update-ref", "refs/heads/backup", oid)

    def snapshot(self):
        result = {}
        for path in [self.root, *self.root.rglob("*")]:
            content = path.read_bytes() if path.is_file() else None
            stat = path.lstat()
            result[str(path.relative_to(self.root))] = (content, stat.st_mode, stat.st_size,
                                                       stat.st_mtime_ns, stat.st_ctime_ns)
        return result

    def close(self):
        self.directory.cleanup()


def transport_fixture(repo, helper_root):
    """Deliberate local setup; helper only writes a marker and exits, no network."""
    helper = helper_root / "git-remote-preview-probe"
    marker = helper_root / "helper-started"
    helper.write_text("#!" + sys.executable + "\nfrom pathlib import Path\n" +
                      "Path(" + repr(str(marker)) + ").write_text('started')\nraise SystemExit(1)\n")
    helper.chmod(0o755)
    repo.git("config", "core.repositoryformatversion", "1")
    repo.git("config", "extensions.partialClone", "origin")
    repo.git("config", "remote.origin.promisor", "true")
    repo.git("config", "remote.origin.url", "preview-probe::generic")
    repo.git("config", "protocol.preview-probe.allow", "always")
    oid = next(oid for oid, item in repo.fixture.objects.items()
               if item["type"] == "blob" and base64.b64decode(item["data"]) == b"base\n")
    (repo.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    return oid, marker


def qualify_local_backend():
    """Real process evidence, generated outside the reader and target repository.

    A successful test probe is not independent production approval. No receipt is
    shipped as a default allowlist; callers own provenance and approval.
    """
    executable = str(Path(shutil.which("git")).resolve())
    observer = shutil.which("strace")
    if observer is None:
        raise AssertionError("direct process observer unavailable; backend unqualified")
    repo = Repository()
    try:
        with tempfile.TemporaryDirectory(prefix="reader-probe-observer-") as directory:
            root = Path(directory)
            oid, marker = transport_fixture(repo, root)
            traces = []
            digest = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
            for disabled in [False, True]:
                trace = root / ("disabled.trace" if disabled else "control.trace")
                environment = dict(os.environ, GIT_NO_LAZY_FETCH=str(int(disabled)),
                                   GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
                                   GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0",
                                   GIT_TERMINAL_PROMPT="0", LC_ALL="C")
                environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
                if disabled:
                    marker.unlink()
                    before = repo.snapshot()
                result = subprocess.run([observer, "-f", "-qq", "-s", "8192", "-e",
                                         "trace=execve,connect", "-o", str(trace), executable,
                                         "--no-pager", "-C", str(repo.root), "cat-file", "--batch-check"],
                                        input=oid.encode() + b"\n", capture_output=True,
                                        env=environment, timeout=20)
                if result.returncode != 0:
                    raise AssertionError("process probe failed: " + result.stderr.decode())
                traces.append(base64.b64encode(trace.read_bytes()).decode("ascii"))
                if not disabled and not marker.exists():
                    raise AssertionError("positive control did not start local transport helper")
                if disabled and (marker.exists() or before != repo.snapshot()):
                    raise AssertionError("flag-on backend attempted transport or changed repository")
            receipt = {"schema": "syncwheel.git-backend-probe.v1", "executableSha256": digest,
                       "controlTrace": traces[0], "disabledTrace": traces[1],
                       "controlHelperStarted": True, "disabledHelperStarted": False,
                       "repositoryUnchanged": True}
            reader._qualified_backend(receipt)
            return receipt
    finally:
        repo.close()


class ReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qualification = qualify_local_backend()

    def setUp(self):
        self.repo = Repository()
        self.addCleanup(self.repo.close)

    def read(self, **kwargs):
        return reader.read_snapshot(self.repo.root, "refs/heads/backup",
                                    qualification=self.qualification, **kwargs)

    def test_native_reader_to_pure_preserves_all_29_ordered_stacks(self):
        self.repo.close()
        self.repo = Repository(count=29)
        self.addCleanup(self.repo.close)
        envelope = self.read()
        manifest = json.loads(base64.b64decode(envelope["frozen"]["backupManifest"]))
        self.assertEqual(manifest, self.repo.fixture.manifest)
        self.assertEqual(envelope["frozen"]["sourcePins"], self.repo.fixture.frozen["sourcePins"])
        plan = pure.preview(envelope["frozen"], envelope["observed"], envelope["objects"], {})
        self.assertEqual(plan["stackCount"], 29)
        self.assertEqual([row["id"] for row in plan["accounting"]], manifest["integration"]["stacks"])
        self.assertEqual(len(plan["changes"]), 29)

    def test_positive_preserves_manifest_fields_pins_objects_and_every_native_path(self):
        before = self.repo.snapshot()
        calls = []
        original = reader._Reader.git

        def record(instance, *args, **kwargs):
            calls.append(args)
            return original(instance, *args, **kwargs)

        with mock.patch.object(reader._Reader, "git", record):
            envelope = self.read()
        self.assertEqual(before, self.repo.snapshot())
        self.assertEqual(envelope["frozen"], self.repo.fixture.frozen)
        self.assertEqual(envelope["frozen"], envelope["observed"])
        self.assertEqual(base64.b64decode(envelope["frozen"]["backupManifest"]),
                         base64.b64decode(self.repo.fixture.frozen["backupManifest"]))
        self.assertFalse(envelope["observation"]["workingTreeIndexObserved"])
        self.assertIn("no atomicity/ABA guarantee", envelope["observation"]["scope"])
        self.assertEqual(envelope, self.read(expected=envelope))
        plan = pure.preview(envelope["frozen"], envelope["observed"], envelope["objects"], {})
        self.assertEqual(plan["stackCount"], 2)
        self.assertTrue(all(args[0] in {"--version", "rev-parse", "config", "for-each-ref", "symbolic-ref", "cat-file"}
                            for args in calls))
        self.assertTrue(all("--filters" not in args and "--textconv" not in args for args in calls))

    def test_sampled_ref_drift_and_later_expected_envelope_drift_reject(self):
        original = reader._Reader.snapshot
        count = 0

        def changing(instance):
            nonlocal count
            result = original(instance)
            count += 1
            if count == 1:
                # Deliberate fixture-writer mutation, not performed by the reader.
                self.repo.git("update-ref", "refs/heads/unrelated", self.repo.fixture.frozen["base"])
            return result

        with mock.patch.object(reader._Reader, "snapshot", changing), self.assertRaises(reader.ReaderError):
            self.read()
        frozen = self.read()
        self.repo.git("update-ref", "refs/heads/another", self.repo.fixture.frozen["base"])
        with self.assertRaises(reader.ReaderError):
            self.read(expected=frozen)

    def test_missing_objects_and_acquisition_bounds_reject(self):
        for kwargs in [{"max_objects": 1}, {"max_bytes": 1}, {"max_tree_entries": 1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(reader.ReaderError):
                self.read(**kwargs)
        oid = next(oid for oid, item in self.repo.fixture.objects.items()
                   if item["type"] == "blob" and base64.b64decode(item["data"]) == b"base\n")
        (self.repo.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
        with self.assertRaises(reader.ReaderError):
            self.read()

    def test_manifest_omission_alternate_recipe_and_pin_drift_reject(self):
        original = copy.deepcopy(self.repo.fixture.manifest)
        omitted = copy.deepcopy(original)
        omitted["integration"]["stacks"].pop()
        self.repo.replace_manifest(omitted)
        with self.assertRaises(reader.ReaderError):
            self.read()
        alternate = copy.deepcopy(original)
        alternate["stacks"][0]["integration_commits"] = ["0" * 40]
        self.repo.replace_manifest(alternate)
        with self.assertRaises(reader.ReaderError):
            self.read()
        self.repo.replace_manifest(original)
        self.repo.git("update-ref", "refs/heads/source-0", self.repo.fixture.frozen["base"])
        with self.assertRaises(reader.ReaderError):
            self.read()

    def test_unsupported_object_views_reject_before_object_reads(self):
        markers = ["shallow", "info/grafts", "objects/info/alternates",
                   "objects/info/http-alternates", "objects/pack/sample.promisor", "commondir"]
        for relative in markers:
            marker = self.repo.root / ".git" / relative
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(b"")
            with self.subTest(marker=relative), self.assertRaises(reader.ReaderError):
                self.read()
            marker.unlink()
        self.repo.git("config", "remote.origin.promisor", "true")
        with self.assertRaises(reader.ReaderError):
            self.read()
        self.repo.git("config", "--unset", "remote.origin.promisor")
        self.repo.git("update-ref", "refs/replace/" + self.repo.fixture.frozen["base"],
                      self.repo.fixture.frozen["backup"])
        with self.assertRaises(reader.ReaderError):
            self.read()

    def test_environment_routing_and_trace_outputs_reject_without_git(self):
        trace = self.repo.root / "trace-output"
        for key, value in [("GIT_NAMESPACE", "other"), ("GIT_TRACE", str(trace)),
                           ("GIT_CONFIG_PARAMETERS", "'core.bare=true'"),
                           ("GIT_CONFIG_COUNT", "0"), ("GIT_INDEX_FILE", "elsewhere")]:
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}), \
                    mock.patch("subprocess.run", side_effect=AssertionError("Git must not start")), \
                    self.assertRaises(reader.ReaderError):
                self.read()
        self.assertFalse(trace.exists())

    def test_configured_trace_outputs_are_disabled_before_first_git_read(self):
        config = self.repo.root / ".git" / "config"
        paths = [self.repo.root / ("trace-" + name) for name in ["normal", "event", "perf"]]
        with config.open("a") as handle:
            handle.write("\n[trace2]\n")
            for name, path in zip(["normalTarget", "eventTarget", "perfTarget"], paths):
                handle.write(f"\t{name} = {path}\n")
        before = self.repo.snapshot()
        self.read()
        self.assertEqual(before, self.repo.snapshot())
        self.assertTrue(all(not path.exists() for path in paths))

    def test_unqualified_or_drifted_backend_rejects_before_any_git_command(self):
        for qualification in [None, {}, dict(self.qualification, executableSha256="0" * 64),
                              dict(self.qualification, disabledTrace=self.qualification["controlTrace"]),
                              dict(self.qualification, controlTrace=self.qualification["disabledTrace"])]:
            with self.subTest(qualification=qualification is None), \
                    mock.patch("subprocess.run", side_effect=AssertionError("Git must not start")), \
                    self.assertRaises(reader.ReaderError):
                reader.read_snapshot(self.repo.root, "backup", qualification=qualification)

    def test_lazy_fetch_race_observes_zero_fetch_helper_or_connection_attempts(self):
        original = reader._Reader.snapshot
        run = subprocess.run
        injected, traces = [], []
        with tempfile.TemporaryDirectory(prefix="reader-race-observer-") as directory:
            observer_root = Path(directory)

            def changing(instance):
                result = original(instance)
                if not injected:
                    transport_fixture(self.repo, observer_root)
                    injected.append(self.repo.snapshot())
                return result

            def observed(command, **kwargs):
                # Instrument ONLY reader commands; fixture setup mutations use
                # literal git without --no-pager and remain outside this evidence.
                if len(command) > 1 and command[1] == "--no-pager":
                    trace = observer_root / ("read-" + str(len(traces)) + ".trace")
                    traces.append(trace)
                    command = [shutil.which("strace"), "-f", "-qq", "-s", "8192", "-e",
                               "trace=execve,connect", "-o", str(trace), *command]
                return run(command, **kwargs)

            with mock.patch.object(reader._Reader, "snapshot", changing), \
                    mock.patch("subprocess.run", observed), self.assertRaises(reader.ReaderError):
                self.read()
            self.assertEqual(injected[0], self.repo.snapshot())
            self.assertTrue(traces)
            combined = "\n".join(path.read_text() for path in traces)
            self.assertIn('"--batch-check"', combined)
            for token in ['"fetch"', 'git-remote-', 'connect(']:
                self.assertNotIn(token, combined)
            self.assertFalse((observer_root / "helper-started").exists())

    def test_tags_ambiguous_names_and_selected_symbolic_refs_reject(self):
        self.repo.git("update-ref", "refs/tags/backup", self.repo.fixture.frozen["backup"])
        for selector in ["backup", "refs/tags/backup", "HEAD~1"]:
            with self.subTest(selector=selector), self.assertRaises(reader.ReaderError):
                reader.read_snapshot(self.repo.root, selector, qualification=self.qualification)
        self.repo.git("symbolic-ref", "refs/heads/source-0", "refs/heads/base")
        with self.assertRaises(reader.ReaderError):
            self.read()

    def test_linked_metadata_and_unknown_extensions_reject(self):
        self.repo.git("config", "extensions.worktreeConfig", "true")
        with self.assertRaises(reader.ReaderError):
            self.read()
        self.repo.git("config", "--unset", "extensions.worktreeConfig")
        metadata = self.repo.root / ".git"
        saved = self.repo.root / "saved-metadata"
        metadata.rename(saved)
        metadata.write_text("gitdir: saved-metadata\n")
        with self.assertRaises(reader.ReaderError):
            self.read()


if __name__ == "__main__":
    unittest.main()
