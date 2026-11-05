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
    def test_commit_header_grammar_rejects_hash_valid_unsupported_objects(self):
        f = Fixture()
        tree = f.tree({"guide.txt": b"base\n"}).encode()
        tree_line = b"tree " + tree
        parent_line = b"parent " + f.frozen["base"].encode()
        author = b"author Example <example@example.invalid> 1700000000 +0000"
        committer = b"committer Example <example@example.invalid> 1700000000 +0000"
        valid = [tree_line, parent_line, author, committer]
        invalid = {
            "missing author": [tree_line, parent_line, committer],
            "missing committer": [tree_line, parent_line, author],
            "missing both identities": [tree_line, parent_line],
            "tree is not first": [author, tree_line, parent_line, committer],
            "identity order": [tree_line, parent_line, committer, author],
            "late parent": [tree_line, author, parent_line, committer],
            "duplicate tree": [tree_line, tree_line, parent_line, author, committer],
            "duplicate author": [tree_line, parent_line, author, author, committer],
            "duplicate parent": [tree_line, parent_line, parent_line, author, committer],
            "extension": valid + [b"encoding UTF-8"],
            "space continuation": valid + [b" continuation"],
            "tab continuation": valid + [b"\tcontinuation"],
            "signature continuation": valid + [b"gpgsig signature", b" continued"],
            "short tree OID": [b"tree abc", parent_line, author, committer],
            "timezone minute": [tree_line, parent_line, author.replace(b"+0000", b"+0060"), committer],
            "noncanonical epoch": [tree_line, parent_line, author.replace(b"1700000000", b"01700000000"), committer],
        }
        for name, headers in invalid.items():
            with self.subTest(name=name):
                oid = f.object("commit", b"\n".join(headers) + b"\n\nmessage\n")
                with self.assertRaises(module.PreviewError):
                    module._Objects(f.objects, "sha1").commit(oid)
        for separator in [b"\r", b"\r\n", b"\v", b"\f", b"\x85", b"\x1c", b"\x00"]:
            with self.subTest(separator=separator):
                raw = separator.join(valid[:2]) + b"\n" + b"\n".join(valid[2:]) + b"\n\nmessage\n"
                oid = f.object("commit", raw)
                with self.assertRaises(module.PreviewError):
                    module._Objects(f.objects, "sha1").commit(oid)
        oid = f.object("commit", b"\n".join(valid) + b"\n\nopaque message\r\v\f\n continuation\n")
        self.assertEqual(module._Objects(f.objects, "sha1").commit(oid),
                         (tree.decode(), [f.frozen["base"]]))

    def test_preview_rejects_hash_valid_source_without_author(self):
        f = Fixture()
        stack = f.manifest["stacks"][0]
        original = base64.b64decode(f.objects[stack["commits"][0]]["data"])
        raw = b"\n".join(line for line in original.split(b"\n") if not line.startswith(b"author "))
        oid = f.object("commit", raw)
        stack["commits"] = [oid]
        manifest = json.dumps(f.manifest).encode()
        f.frozen.update(sourcePins={s["branch"]: s["commits"][-1] for s in f.manifest["stacks"]},
                        backupManifest=b64(manifest), backupManifestSha256=hashlib.sha256(manifest).hexdigest(),
                        backup=f.commit({".syncwheel/manifest.json": manifest}, f.frozen["base"]))
        with self.assertRaises(module.PreviewError):
            f.plan()

    def test_representable_trees_round_trip_and_empty_subtrees_reject(self):
        f = Fixture()
        for files in [{}, {"guide.txt": b"base\n", "nested/name.txt": b"\x00\xff\n"}]:
            with self.subTest(files=list(files)):
                tree = f.tree(files)
                store = module._Objects(f.objects, "sha1")
                flattened = store.files(tree)
                self.assertEqual(store.tree_oid(flattened), tree)
                self.assertEqual({p: data for p, (_, data) in flattened.items()}, files)
        empty = f.object("tree", b"")
        child = f.object("tree", b"40000 empty\0" + bytes.fromhex(empty))
        mixed = f.object("tree", b"40000 empty\0" + bytes.fromhex(empty)
                         + b"100644 file.txt\0" + bytes.fromhex(f.object("blob", b"data\n")))
        nested = f.object("tree", b"40000 nested\0" + bytes.fromhex(child))
        for tree in [child, mixed, nested]:
            with self.subTest(tree=tree), self.assertRaises(module.PreviewError):
                module._Objects(f.objects, "sha1").files(tree)
        f.frozen["base"] = f.object("commit", b"tree " + child.encode()
                                   + b"\nauthor Example <example@example.invalid> 1700000000 +0000\n"
                                   + b"committer Example <example@example.invalid> 1700000000 +0000\n\nfixture\n")
        with self.assertRaises(module.PreviewError):
            f.plan()

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
