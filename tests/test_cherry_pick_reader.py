"""Generic native-object fixtures for the reader; no real recovery is exercised."""
import base64
import copy
import hashlib
import json
import os
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
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="reader-object-fixture-")
        self.root = Path(self.directory.name)
        self.fixture = Fixture(count=2)
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


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository()
        self.addCleanup(self.repo.close)

    def read(self, **kwargs):
        return reader.read_snapshot(self.repo.root, "refs/heads/backup", **kwargs)

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
        for kwargs in [{"max_objects": 1}, {"max_bytes": 1}]:
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

    def test_lazy_fetch_stays_disabled_if_fixture_view_changes_after_first_sample(self):
        original = reader._Reader.snapshot
        injected = []

        def changing(instance):
            result = original(instance)
            if not injected:
                # External fixture-writer change is intentionally outside the supported
                # cooperative read scope. Probe the no-lazy-fetch defense independently.
                self.repo.git("config", "core.repositoryformatversion", "1")
                self.repo.git("config", "extensions.partialClone", "origin")
                self.repo.git("config", "remote.origin.promisor", "true")
                self.repo.git("config", "remote.origin.url", str(self.repo.root / "absent-remote"))
                oid = next(oid for oid, item in self.repo.fixture.objects.items()
                           if item["type"] == "blob" and base64.b64decode(item["data"]) == b"base\n")
                (self.repo.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
                injected.append(self.repo.snapshot())
            return result

        with mock.patch.object(reader._Reader, "snapshot", changing), self.assertRaises(reader.ReaderError):
            self.read()
        self.assertEqual(injected[0], self.repo.snapshot())

    def test_tags_ambiguous_names_and_selected_symbolic_refs_reject(self):
        self.repo.git("update-ref", "refs/tags/backup", self.repo.fixture.frozen["backup"])
        for selector in ["backup", "refs/tags/backup", "HEAD~1"]:
            with self.subTest(selector=selector), self.assertRaises(reader.ReaderError):
                reader.read_snapshot(self.repo.root, selector)
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
