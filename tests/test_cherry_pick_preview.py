"""Synthetic qualification candidates; execute only on an authorized test surface."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "pure_cherry_preview", Path(__file__).parents[1] / "scripts" / "syncwheel_cherry_pick_preview.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def b64(data):
    return base64.b64encode(data).decode("ascii")


class Fixture:
    def __init__(self, count=2, overlap=False):
        self.objects = {}
        initial = {f"file-{i}.txt": b"base\n" for i in range(1 if overlap else count)}
        self.base_files = initial
        base = self.commit(initial, None)
        stacks = []
        for i in range(count):
            changed = dict(initial)
            path = f"file-{0 if overlap else i}.txt"
            changed[path] += f"addition-{i}\n".encode()
            oid = self.commit(changed, base)
            stacks.append({"id": f"step-{i}", "branch": f"source-{i}", "base": "base",
                           "commits": [oid], "meta": {"opaque": {"ordinal": i}}})
        manifest = {"version": 1, "stacks": stacks,
                    "integration": {"base": "base", "branch": "integration",
                                    "strategy": "cherry-pick", "stacks": [s["id"] for s in stacks]}}
        raw = json.dumps(manifest, indent=2).encode() + b"\n"
        backup = self.commit({".syncwheel/manifest.json": raw}, base)
        self.frozen = {"primary": base, "base": base, "backup": backup,
                       "backupManifest": b64(raw), "backupManifestSha256": hashlib.sha256(raw).hexdigest(),
                       "sourcePins": {s["branch"]: s["commits"][-1] for s in stacks}, "objectFormat": "sha1"}
        self.manifest = manifest
        self.witnesses = {}
        if overlap:
            current, source = b"base\naddition-0\n", b"base\naddition-1\n"
            key = stacks[1]["commits"][0] + ":file-0.txt"
            self.witnesses[key] = {"preimage": b64(current), "sourcePreimage": b64(b"base\n"),
                                   "sourcePostimage": b64(source), "postimage": b64(current + b"addition-1\n"),
                                   "currentAddition": b64(b"addition-0\n"), "sourceAddition": b64(b"addition-1\n")}

    def object(self, kind, raw):
        oid = hashlib.sha1(kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        self.objects[oid] = {"type": kind, "data": b64(raw)}
        return oid

    def tree(self, files):
        node = {}
        for path, value in files.items():
            top, sep, rest = path.partition("/")
            if sep:
                node.setdefault(top, {})[rest] = value
            else:
                node[top] = value
        entries = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            mode = b"40000" if directory else b"100644"
            oid = self.tree(value) if directory else self.object("blob", value)
            entries.append((name.encode() + (b"/" if directory else b""),
                            mode + b" " + name.encode() + b"\0" + bytes.fromhex(oid)))
        return self.object("tree", b"".join(entry for _, entry in sorted(entries)))

    def commit(self, files, parent):
        raw = b"tree " + self.tree(files).encode() + b"\n"
        if parent:
            raw += b"parent " + parent.encode() + b"\n"
        raw += b"author Example <example@example.invalid> 1700000000 +0000\n"
        raw += b"committer Example <example@example.invalid> 1700000000 +0000\n\nfixture\n"
        return self.object("commit", raw)

    def plan(self, observed=None):
        return module.preview(self.frozen, self.frozen if observed is None else observed,
                              self.objects, self.witnesses)


class PurePreviewTests(unittest.TestCase):
    def test_all_stacks_accounted_and_all_opaque_inputs_bound(self):
        f = Fixture(count=29)
        report = f.plan()
        self.assertEqual(report["stackCount"], 29)
        self.assertEqual(len(report["accounting"]), 29)
        self.assertEqual(len(report["changes"]), 29)
        self.assertFalse(report["applyImplemented"])
        self.assertEqual(report, f.plan())
        extra = f.object("blob", b"unused but frozen input\n")
        self.assertNotEqual(report["planDigest"], f.plan()["planDigest"])
        del f.objects[extra]
        self.assertEqual(report["planDigest"], f.plan()["planDigest"])

    def test_preview_has_no_io_and_preserves_every_input_byte(self):
        f = Fixture(overlap=True)
        before = copy.deepcopy((f.frozen, f.objects, f.witnesses))
        with mock.patch("builtins.open", side_effect=AssertionError("file IO")), \
                mock.patch("subprocess.run", side_effect=AssertionError("process IO")), \
                mock.patch("pathlib.Path.write_bytes", side_effect=AssertionError("write")), \
                mock.patch("pathlib.Path.write_text", side_effect=AssertionError("write")):
            report = f.plan()
        self.assertEqual(before, (f.frozen, f.objects, f.witnesses))
        self.assertEqual(report["semantics"], "explicit-additive-resolution")
        self.assertEqual(base64.b64decode(report["changes"][0]["postimage"]["bytes"]),
                         b"base\naddition-0\naddition-1\n")

    def test_drift_missing_object_witness_loss_and_unwitnessed_overlap_reject(self):
        f = Fixture(overlap=True)
        observed = copy.deepcopy(f.frozen)
        observed["sourcePins"]["source-0"] = "0" * 40
        with self.assertRaises(module.PreviewError):
            f.plan(observed)
        for key in list(f.objects)[:1]:
            item = f.objects.pop(key)
            with self.assertRaises(module.PreviewError):
                f.plan()
            f.objects[key] = item
        witness = next(iter(f.witnesses.values()))
        witness["postimage"] = b64(b"base\naddition-1\n")
        with self.assertRaises(module.PreviewError):
            f.plan()
        f.witnesses.clear()
        with self.assertRaises(module.PreviewError):
            f.plan()

    def test_manifest_omission_and_alternate_recipe_reject(self):
        for mutation in [lambda m: m["integration"]["stacks"].pop(),
                         lambda m: m["stacks"][0].update(integration_commits=["0" * 40])]:
            f = Fixture()
            mutation(f.manifest)
            raw = json.dumps(f.manifest).encode()
            f.frozen.update(backupManifest=b64(raw), backupManifestSha256=hashlib.sha256(raw).hexdigest(),
                            backup=f.commit({".syncwheel/manifest.json": raw}, f.frozen["base"]))
            with self.assertRaises(module.PreviewError):
                f.plan()

    def test_object_bytes_and_witness_preimage_drift_reject(self):
        f = Fixture(overlap=True)
        key = next(iter(f.objects))
        original = f.objects[key]["data"]
        f.objects[key]["data"] = b64(b"altered bytes\n")
        with self.assertRaises(module.PreviewError):
            f.plan()
        f.objects[key]["data"] = original
        witness = next(iter(f.witnesses.values()))
        witness["preimage"] = b64(b"unrelated\n")
        with self.assertRaises(module.PreviewError):
            f.plan()

    def test_exact_preimage_subset_matches_git_and_does_not_change_repository(self):
        f = Fixture(count=2)
        with tempfile.TemporaryDirectory(prefix="pure-replay-oracle-") as directory:
            root = Path(directory)
            def git(*args, data=None):
                return subprocess.run(["git", "-C", str(root), *args], input=data,
                                      capture_output=True, check=True).stdout
            git("init", "--quiet")
            git("config", "user.name", "Example")
            git("config", "user.email", "example@example.invalid")
            for oid, item in f.objects.items():
                written = git("hash-object", "-w", "-t", item["type"], "--stdin", data=base64.b64decode(item["data"]))
                self.assertEqual(written.decode().strip(), oid)
            git("checkout", "--quiet", f.frozen["base"])
            for stack in f.manifest["stacks"]:
                git("-c", "commit.gpgsign=false", "cherry-pick", stack["commits"][0])
            expected = git("rev-parse", "HEAD^{tree}").decode().strip()
            def snapshot():
                result = {}
                for path in root.rglob("*"):
                    if path.is_file():
                        content, stat = path.read_bytes(), path.stat()
                        result[str(path.relative_to(root))] = (content, stat.st_mode,
                                                              stat.st_mtime_ns, stat.st_ctime_ns)
                return result
            before = snapshot()
            report = f.plan()
            self.assertEqual(before, snapshot())
            self.assertEqual(report["resultTree"], expected)
            self.assertEqual(report["semantics"], "exact-preimage")


if __name__ == "__main__":
    unittest.main()
