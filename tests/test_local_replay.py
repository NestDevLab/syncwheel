import hashlib
import json
import os
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


CLI = Path(__file__).resolve().parents[1] / "scripts" / "syncwheel.py"


@unittest.skipUnless(sys.platform == "linux", "local replay requires Linux")
class LocalReplayTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="syncwheel-local-fixture-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(PYTHONDONTWRITEBYTECODE="1", HOME=temp.name,
                        XDG_CONFIG_HOME=str(Path(temp.name) / "config"), GIT_ALLOW_PROTOCOL="")
        self.git("-c", "init.defaultRefFormat=files", "init", "--object-format=sha1", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.file("feature.txt", "old feature\n")
        self.file("detail.txt", "old detail\n")
        self.file("unrelated.txt", "common content\n")
        self.commit("Base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "feature")
        self.file("feature.txt", "new feature\n")
        self.file("detail.txt", "new detail\n")
        self.commit("Owned change")
        self.source = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "-c", "integration", "main")
        self.file("unrelated.txt", "integration contribution\n")
        self.file(".syncwheel/manifest.json", json.dumps({
            "version": 1, "syncwheel_tracking": "git-tracked", "repository_mode": "delivery",
            "authority": {"mode": "ai-managed", "allow": ["source_change"], "deny": ["destructive_rewrite"]},
            "defaults": {"canonical_remote": "origin", "publication_remote": "origin", "base_branch": "main",
                         "base_ref": self.base, "integration_membership": "required"},
            "integration": {"branch": "integration", "base": self.base, "strategy": "cherry-pick",
                            "stacks": ["feature"]},
            "stacks": [{"id": "feature", "branch": "feature", "base": self.base, "commits": [self.source]}],
        }) + "\n")
        self.commit("Integration baseline")
        self.old = self.git("rev-parse", "HEAD")
        # Qualify only the filesystem capability needed by the positive cases.
        probe = Path(temp.name) / "probe"
        probe.mkdir()
        (probe / "a").write_bytes(b"a")
        (probe / "b").write_bytes(b"b")
        result = subprocess.run(
            [sys.executable, "-c", "import os,sys; from syncwheel_local_replay import exchange; "
             "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); exchange(fd,'a',fd,'b')", str(probe)],
            env={**self.env, "PYTHONPATH": str(CLI.parent)}, text=True, capture_output=True,
        )
        if result.returncode and any(reason in result.stderr for reason in (
            "Function not implemented", "Operation not supported", "requires atomic Linux file exchange",
        )):
            self.skipTest("atomic file exchange unavailable")
        self.assertEqual(result.returncode, 0, result.stderr)

    def git(self, *args, expected=0):
        result = subprocess.run(["git", *args], cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result.stdout.strip()

    def file(self, path, content):
        target = self.root / path
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def cli(self, *args, expected=0, crash=None, checkpoint_code=None, module_setup=None):
        if crash or checkpoint_code or module_setup:
            source = checkpoint_code or f"if phase == {crash!r}: os._exit(87)"
            runner = (
                "import os,sys,subprocess,signal; from pathlib import Path; "
                "sys.path.insert(0,sys.argv.pop(1)); import syncwheel,syncwheel_local_replay;\n"
                + (module_setup + "\n" if module_setup else "") +
                "def callback(phase):\n " + source.replace("\n", "\n ") + "\n"
                "syncwheel_local_replay.checkpoint=callback; sys.exit(syncwheel.main())"
            )
            command = [sys.executable, "-c", runner, str(CLI.parent)]
        else:
            command = [sys.executable, str(CLI)]
        result = subprocess.run(
            [*command, "int", "replay-local", "feature", "--repo", str(self.root), *args],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=25, start_new_session=True,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout) if result.returncode == 0 else result.stderr

    def apply(self, plan, **kwargs):
        return self.cli("--apply", "--plan-digest", plan["planDigest"], "--reason", "fixture", **kwargs)

    def recover(self, plan, **kwargs):
        return self.cli("--recover", "--plan-digest", plan["planDigest"], **kwargs)

    def content_snapshot(self):
        return {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    def local_source_setup(self, coordinated=False):
        path = self.root / ".syncwheel/manifest.json"
        data = json.loads(path.read_text())
        data["stacks"][0]["branch"] = "pr/materialized-feature"
        if coordinated:
            data["version"] = 2
            data["coordination"] = {"mode": "active-active", "id": "fixture", "remote": "origin", "state_branch": "syncwheel/state/fixture"}
        path.write_text(json.dumps(data) + "\n")
        self.commit("Declare missing exact source ref")
        self.materialized_ref = "refs/heads/pr/materialized-feature"

    def materialize_cli(self, *args, expected=0, checkpoint_code=None):
        command = [sys.executable, str(CLI)]
        if checkpoint_code:
            runner = ("import os,sys;sys.path.insert(0,sys.argv.pop(1));import syncwheel,syncwheel_local_replay;\n"
                      "def callback(phase):\n " + checkpoint_code.replace("\n", "\n ") + "\n"
                      "syncwheel_local_replay.checkpoint=callback;sys.exit(syncwheel.main())")
            command = [sys.executable, "-c", runner, str(CLI.parent)]
        result = subprocess.run([*command, "stack", "materialize-local", "feature", "--repo", str(self.root), *args],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=25)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout) if expected == 0 else result.stderr

    def materialize_apply(self, plan, **kwargs):
        return self.materialize_cli("--apply", "--plan-digest", plan["planDigest"], "--reason", "fixture", **kwargs)

    def test_local_source_materialization_is_ref_only_and_works_without_remote(self):
        self.local_source_setup(coordinated=True)
        before = self.content_snapshot()
        plan = self.materialize_cli()
        self.assertEqual(self.content_snapshot(), before)
        head, index = self.git("rev-parse", "HEAD"), (self.root / ".git/index").read_bytes()
        outcome = self.materialize_apply(plan)
        self.assertTrue(outcome["created"])
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.source)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        for path in ["feature.txt", "detail.txt", "unrelated.txt", ".syncwheel/manifest.json"]:
            self.assertEqual(hashlib.sha256((self.root / path).read_bytes()).hexdigest(), before[path])

    def test_local_source_materialization_preserves_existing_and_recreated_refs(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.git("update-ref", self.materialized_ref, self.old)
        self.assertIn("existing different target", self.materialize_apply(plan, expected=2))
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.old)

    def test_local_source_materialization_preserves_group_writable_read_only_files(self):
        self.local_source_setup()
        path = self.root / "unrelated.txt"
        path.chmod(0o664)
        original = path.read_bytes(), path.stat()
        plan = self.materialize_cli()
        self.assertTrue(self.materialize_apply(plan)["created"])
        current = path.stat()
        self.assertEqual(path.read_bytes(), original[0])
        self.assertEqual((current.st_dev, current.st_ino, current.st_mode, current.st_mtime_ns),
                         (original[1].st_dev, original[1].st_ino, original[1].st_mode, original[1].st_mtime_ns))

    def test_local_source_materialization_preserves_group_writable_index_and_rejects_mode_drift(self):
        self.local_source_setup()
        index = self.root / '.git/index'
        index.chmod(0o664)
        before = index.read_bytes(), index.stat()
        plan = self.materialize_cli()
        self.assertEqual(plan['index']['mode'], 0o664)
        index.chmod(0o644)
        self.materialize_apply(plan, expected=2)
        self.git('rev-parse', '--verify', '--quiet', self.materialized_ref, expected=1)
        index.chmod(0o664)
        self.assertTrue(self.materialize_apply(plan)['created'])
        current = index.stat()
        self.assertEqual(index.read_bytes(), before[0])
        self.assertEqual((current.st_dev, current.st_ino, current.st_mode, current.st_mtime_ns),
                         (before[1].st_dev, before[1].st_ino, before[1].st_mode, before[1].st_mtime_ns))

    def test_local_source_materialization_rejects_drift_and_dirty_checkout(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.file("unrelated.txt", "concurrent WIP\n")
        self.assertIn("checkout differs", self.materialize_apply(plan, expected=2))
        self.assertEqual((self.root / "unrelated.txt").read_text(), "concurrent WIP\n")
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)

    def test_local_source_materialization_post_create_crash_is_inspectable_without_retry(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.materialize_apply(plan, expected=87, checkpoint_code="if phase == 'source-created': os._exit(87)")
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.source)
        observed = self.materialize_cli()
        self.assertIn("explicit recovery", self.materialize_apply(observed, expected=2))
        outcome = self.materialize_cli("--recover", "--plan-digest", plan["planDigest"])
        self.assertEqual(outcome["status"], "held")
        self.assertFalse(outcome["retryAllowed"])
        self.assertTrue(outcome["indexLeaseReleased"])
        self.assertFalse((self.root / ".git/index.lock").exists())
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.source)

    def test_local_source_materialization_cas_preserves_concurrent_ref_creation(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        code = "if phase == 'source-create-cas': syncwheel_local_replay.git(Path.cwd(),'update-ref'," + repr(self.materialized_ref) + "," + repr(self.old) + ",'0'*40)"
        # The fixture callback is trusted operator test code, never a runtime input.
        code = code.replace("Path.cwd()", "__import__('pathlib').Path.cwd()")
        self.assertIn("create-only CAS refused", self.materialize_apply(plan, expected=2, checkpoint_code=code))
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.old)

    def test_local_source_materialization_refuses_authority_and_non_linear_input(self):
        self.local_source_setup()
        path = self.root / ".syncwheel/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["authority"]["mode"] = "human-gated"
        manifest["authority"]["allow"] = []
        path.write_text(json.dumps(manifest) + "\n")
        self.commit("Gate source changes")
        self.assertIn("requires source-change authority", self.materialize_cli(expected=2))
        manifest["authority"]["mode"] = "ai-managed"
        manifest["authority"]["allow"] = ["source_change"]
        manifest["stacks"][0]["commits"] = [self.source, self.source]
        path.write_text(json.dumps(manifest) + "\n")
        self.commit("Declare invalid repeated ancestry")
        self.assertIn("exact linear source sequence", self.materialize_cli(expected=2))
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)

    def test_local_source_materialization_requires_fresh_manifest_and_index(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        path = self.root / ".syncwheel/manifest.json"
        manifest = json.loads(path.read_text());manifest["stacks"][0]["meta"] = {"purpose": "changed purpose"}
        path.write_text(json.dumps(manifest) + "\n");self.commit("Concurrent metadata change")
        self.assertIn("current digest", self.materialize_apply(plan, expected=2))
        fresh = self.materialize_cli()
        self.file("unrelated.txt", "staged WIP\n");self.git("add", "unrelated.txt")
        self.assertIn("index differs", self.materialize_apply(fresh, expected=2))
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)

    def test_local_source_materialization_records_real_commands_without_network_or_checkout(self):
        self.local_source_setup(coordinated=True)
        real_git = shutil.which("git"); launcher = self.root.parent / "trace-source/git"
        launcher.parent.mkdir(); trace = self.root.parent / "source-calls.jsonl"
        launcher.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
                            f"with open({str(trace)!r},'a') as out:out.write(json.dumps(sys.argv[1:])+'\\n')\n"
                            f"os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n")
        launcher.chmod(0o755); self.env["PATH"] = str(launcher.parent) + os.pathsep + self.env["PATH"]
        plan = self.materialize_cli(); self.assertTrue(self.materialize_apply(plan)["created"])
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any("update-ref" in call and "--stdin" in call for call in calls))
        self.assertTrue(all(call[call.index("worktree") + 1] == "list" for call in calls if "worktree" in call))
        self.assertFalse(any(any(word in call for word in ("fetch", "--prune", "reset", "push", "checkout", "switch", "update-index", "commit-tree", "hash-object", "--force")) for call in calls))

    def test_local_source_materialization_uses_the_existing_managed_reference_hook(self):
        self.local_source_setup()
        result = subprocess.run([sys.executable, str(CLI), "hooks", "install", "--repo", str(self.root), "--apply"],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.materialize_cli()
        self.assertIsNotNone(plan["referenceHook"])
        self.assertTrue(self.materialize_apply(plan)["created"])
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.source)

    def test_local_source_materialization_preserves_a_concurrent_dangling_symbolic_ref(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        foreign = "refs/heads/foreign-unborn"
        code = "if phase == 'source-create-cas': syncwheel_local_replay.git(__import__('pathlib').Path.cwd(),'symbolic-ref'," + repr(self.materialized_ref) + "," + repr(foreign) + ")"
        self.materialize_apply(plan, expected=2, checkpoint_code=code)
        self.assertEqual(self.git("symbolic-ref", self.materialized_ref), foreign)
        self.git("rev-parse", "--verify", "--quiet", foreign, expected=1)

    def test_local_source_materialization_refuses_head_drift_before_effects(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        code = "if phase == 'source-create-cas': syncwheel_local_replay.git(__import__('pathlib').Path.cwd(),'update-ref','HEAD'," + repr(self.base) + ")"
        self.materialize_apply(plan, expected=2, checkpoint_code=code)
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_local_source_materialization_never_recreates_a_ref_after_lost_outcome(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.materialize_apply(plan, expected=87, checkpoint_code="if phase == 'source-created': os._exit(87)")
        self.git("update-ref", "-d", self.materialized_ref, self.source)
        self.assertIn("no automatic re-creation", self.materialize_apply(plan, expected=2))
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)
        recovered = self.materialize_cli("--recover", "--plan-digest", plan["planDigest"])
        self.assertEqual(recovered["status"], "held");self.assertFalse(recovered["retryAllowed"])
        self.assertIn("no automatic re-creation", self.materialize_apply(plan, expected=2))
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)

    def test_local_source_recovery_preserves_a_foreign_index_lock(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.materialize_apply(plan, expected=87, checkpoint_code="if phase == 'source-created': os._exit(87)")
        lock = self.root / ".git/index.lock"
        lock.unlink();lock.write_bytes(b"foreign index lock\n")
        self.assertIn("locked by another writer", self.materialize_cli("--recover", "--plan-digest", plan["planDigest"], expected=2))
        self.assertEqual(lock.read_bytes(), b"foreign index lock\n")

    def test_local_source_materialization_can_create_the_base_of_an_empty_owned_stack(self):
        self.local_source_setup()
        path = self.root / ".syncwheel/manifest.json";data = json.loads(path.read_text())
        data["stacks"][0]["commits"] = [];path.write_text(json.dumps(data) + "\n")
        self.commit("Declare empty source stack")
        plan = self.materialize_cli()
        self.assertEqual(plan["sourceTip"], self.base)
        self.assertTrue(self.materialize_apply(plan)["created"])
        self.assertEqual(self.git("rev-parse", self.materialized_ref), self.base)

    def test_local_source_materialization_holds_native_head_and_index_leases(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        code = """if phase == 'source-ref-prepared':
 for args in [('update-ref','HEAD',BASE),('add','feature.txt')]:
  result=syncwheel_local_replay.subprocess.run(['git',*args],cwd=__import__('pathlib').Path.cwd(),env=syncwheel_local_replay.git_env(),capture_output=True)
  if result.returncode==0: raise RuntimeError('competing native writer won')
""".replace('BASE', repr(self.base))
        self.assertTrue(self.materialize_apply(plan, checkpoint_code=code)["created"])
        self.assertFalse((self.root / ".git/index.lock").exists())

    def test_local_source_recovery_does_not_claim_release_before_lock_acquisition(self):
        self.local_source_setup()
        plan = self.materialize_cli()
        self.materialize_apply(plan, expected=87, checkpoint_code="if phase == 'source-intent-prepared': os._exit(87)")
        self.assertFalse((self.root / ".git/index.lock").exists())
        recovered = self.materialize_cli("--recover", "--plan-digest", plan["planDigest"])
        self.assertEqual(recovered["status"], "held")
        self.assertFalse(recovered["retryAllowed"])
        self.assertFalse(recovered["indexLeaseReleased"])
        self.git("rev-parse", "--verify", "--quiet", self.materialized_ref, expected=1)

    def source_additions(self, payload=None):
        self.git("switch", "-q", "feature")
        added = [f"new/package/nested/item-{number}.txt" for number in range(16)] + ["standalone.txt"]
        for number, path in enumerate(added):
            self.file(path, f"added content {number}\n" if payload is None else payload)
        self.commit("Add owned source files")
        self.source = self.git("rev-parse", "HEAD")
        commits = self.git("rev-list", "--reverse", self.base + "..HEAD").splitlines()
        self.git("switch", "-q", "integration")
        manifest_path = self.root / ".syncwheel/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["stacks"][0]["commits"] = commits
        self.file(".syncwheel/manifest.json", json.dumps(manifest) + "\n")
        self.commit("Declare exact source additions")
        self.old = self.git("rev-parse", "HEAD")
        return added

    def wait_ref_locks(self):
        root = self.root / ".git"
        for _ in range(60):
            if not list((root / "refs").rglob("*.lock")) and not (root / "HEAD.lock").exists():
                return
            time.sleep(0.05)
        self.fail("Git did not release its transaction leases after process exit")

    def test_read_only_preview_and_preservation(self):
        self.file("scratch.txt", "unrelated worktree data\n")
        self.git("branch", "foreign")
        foreign = self.root.parent / "foreign"
        self.git("worktree", "add", "-q", str(foreign), "foreign")
        (foreign / "feature.txt").write_text("foreign WIP\n")
        # A foreign registry is never loaded, repaired, or rewritten by replay.
        self.file(".git/syncwheel/governed-worktrees.json", "unavailable foreign registry\n")
        before = self.content_snapshot()
        plan = self.cli()
        self.assertEqual(self.content_snapshot(), before)
        self.assertEqual(self.cli(), plan)
        self.assertEqual(set(plan["witness"]["paths"]), {"feature.txt", "detail.txt"})
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(self.git("rev-parse", "HEAD"), receipt["after"])
        self.assertEqual(self.git("rev-parse", "HEAD^^"), self.old)
        self.assertEqual(self.git("rev-parse", "feature"), self.source)
        self.assertEqual(self.git("rev-parse", "foreign"), self.old)
        self.assertEqual((foreign / "feature.txt").read_text(), "foreign WIP\n")
        self.assertEqual((self.root / ".git/syncwheel/governed-worktrees.json").read_text(),
                         "unavailable foreign registry\n")
        self.assertEqual((self.root / "scratch.txt").read_text(), "unrelated worktree data\n")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "integration contribution\n")
        self.assertEqual((self.root / "feature.txt").read_text(), "new feature\n")
        manifest = json.loads((self.root / ".syncwheel/manifest.json").read_text())
        self.assertEqual(manifest["stacks"][0]["integration_resolution"]["source_tip"], self.source)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.recover(plan), receipt)

    def test_conflict_and_manifest_drift_refused(self):
        plan = self.cli()
        manifest = self.root / ".syncwheel/manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        self.assertIn("checkout differs", self.apply(plan, expected=2))
        self.file("feature.txt", "integration conflicts\n")
        self.commit("Change the same path")
        self.assertIn("conflict", self.cli(expected=2))

    def test_explicit_files_reference_backend_is_supported(self):
        self.git("config", "extensions.refStorage", "files")
        plan = self.cli()
        self.assertEqual(self.apply(plan)["status"], "completed")

    def test_reftable_reference_configuration_is_refused(self):
        self.git("config", "extensions.refStorage", "reftable")
        # Old Git may ignore this extension in a version-zero fixture, but the
        # executor must never silently reinterpret the declared backend.
        self.assertIn("files reference backend", self.cli(expected=2))

    def test_seventeen_additions_preserve_the_full_tree_and_source(self):
        added = self.source_additions()
        before = self.content_snapshot()
        plan = self.cli()
        self.assertEqual(self.content_snapshot(), before)
        self.assertEqual(set(plan["witness"]["additions"]), {"new", "standalone.txt"})
        receipt = self.apply(plan)
        self.assertEqual(receipt["status"], "completed")
        for number, path in enumerate(added):
            self.assertEqual((self.root / path).read_text(), f"added content {number}\n")
        self.assertEqual(self.git("rev-parse", "feature"), self.source)
        self.assertEqual(self.git("rev-parse", "HEAD^^"), self.old)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual((self.root / "unrelated.txt").read_text(), "integration contribution\n")

    def test_addition_crash_rolls_back_to_absence_without_deleting_source(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        self.assertTrue((self.root / "new").is_dir())
        self.assertEqual(self.recover(plan)["status"], "aborted")
        self.assertFalse((self.root / "new").exists())
        self.assertFalse((self.root / "standalone.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "feature"), self.source)

    def test_addition_recovery_after_committed_ref_is_readback_only(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="ref-committed", expected=87)
        self.wait_ref_locks()
        inode = (self.root / "new").stat().st_ino
        after = self.git("rev-parse", "HEAD")
        self.assertEqual(self.recover(plan)["after"], after)
        self.assertEqual((self.root / "new").stat().st_ino, inode)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_addition_collision_is_refused_before_any_effect(self):
        self.source_additions()
        plan = self.cli()
        self.file("new/untracked.txt", "foreign work\n")
        self.assertIn("collides", self.apply(plan, expected=2))
        self.assertEqual((self.root / "new/untracked.txt").read_text(), "foreign work\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertFalse((self.root / ".git/syncwheel-local-replay").exists())

    def test_addition_race_under_ref_leases_preserves_untracked_data(self):
        self.source_additions()
        plan = self.cli()
        script = """if phase == 'ref-transaction-prepared':
 Path('new').mkdir()
 Path('new/untracked.txt').write_text('foreign work')
"""
        self.assertIn("collides", self.apply(plan, expected=2, checkpoint_code=script))
        self.wait_ref_locks()
        self.assertEqual(self.recover(plan)["status"], "aborted")
        self.assertEqual((self.root / "new/untracked.txt").read_text(), "foreign work")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)

    def test_addition_recovery_keeps_concurrent_new_content(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        self.file("new/untracked.txt", "foreign work\n")
        self.assertIn("preserving the journal", self.recover(plan, expected=2))
        self.assertEqual((self.root / "new/untracked.txt").read_text(), "foreign work\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)

    def test_identical_recreated_addition_is_not_claimed_as_owned(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        retained = self.root.parent / "original-new-tree"
        (self.root / "new").rename(retained)
        shutil.copytree(retained, self.root / "new")
        self.assertIn("inode changed", self.recover(plan, expected=2))
        self.assertTrue(retained.is_dir())
        self.assertEqual((self.root / "new/package/nested/item-0.txt").read_text(), "added content 0\n")

    def test_identical_recreated_descendant_file_is_not_removed_from_checkout(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        root_inode = (self.root / "new").stat().st_ino
        child = self.root / "new/package/nested/item-0.txt"
        retained = self.root.parent / "original-added-file"
        child.rename(retained)
        child.write_bytes(retained.read_bytes())
        self.assertNotEqual(child.stat().st_ino, retained.stat().st_ino)
        self.assertEqual((self.root / "new").stat().st_ino, root_inode)
        self.assertIn("inode changed", self.recover(plan, expected=2))
        self.assertEqual(child.read_bytes(), retained.read_bytes())
        self.assertEqual((self.root / "new").stat().st_ino, root_inode)

    def test_identical_recreated_descendant_directory_is_not_removed_from_checkout(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        child = self.root / "new/package/nested"
        retained = self.root.parent / "original-added-subdirectory"
        child.rename(retained)
        shutil.copytree(retained, child)
        self.assertIn("inode changed", self.recover(plan, expected=2))
        self.assertTrue(child.is_dir())
        self.assertEqual((child / "item-0.txt").read_bytes(), (retained / "item-0.txt").read_bytes())

    def test_reused_descendant_inode_is_not_treated_as_the_prepared_file(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        child = self.root / "new/package/nested/item-0.txt"
        original_inode, data = child.stat().st_ino, child.read_bytes()
        for _ in range(256):
            child.unlink()
            child.write_bytes(data)
            if child.stat().st_ino == original_inode:
                break
        else:
            self.skipTest("fixture filesystem did not reuse the original inode")
        self.assertIn("identity changed", self.recover(plan, expected=2))
        self.assertTrue(child.is_file())
        self.assertEqual(child.read_bytes(), data)

    def test_missing_native_handle_support_refuses_preview_without_effects(self):
        self.source_additions()
        before = self.content_snapshot()
        result = self.cli(expected=2, module_setup=(
            "syncwheel_local_replay.ctypes.CDLL=lambda *args,**kwargs: object()"
        ))
        self.assertIn("require native filesystem handles", result)
        self.assertEqual(self.content_snapshot(), before)

    def test_recovery_refuses_a_legacy_addition_without_generation_witnesses(self):
        self.source_additions()
        plan = self.cli()
        self.apply(plan, crash="checkout-addition-aligned", expected=87)
        self.wait_ref_locks()
        journal = self.root / ".git/syncwheel-local-replay" / plan["planDigest"] / "journal.json"
        record = json.loads(journal.read_text())
        record["slots"]["new"]["identity"].pop("fileHandle")
        journal.write_text(json.dumps(record))
        before = self.content_snapshot()
        self.assertIn("valid inode witness", self.recover(plan, expected=2))
        self.assertEqual(self.content_snapshot(), before)

    def test_reused_flat_file_inode_is_not_removed_from_checkout(self):
        self.source_additions()
        plan = self.cli()
        script = """if phase == 'checkout-addition-aligned':
 globals()['addition_count']=globals().get('addition_count',0)+1
 if globals()['addition_count']==2: os._exit(87)
"""
        self.apply(plan, checkpoint_code=script, expected=87)
        self.wait_ref_locks()
        child = self.root / "standalone.txt"
        original_inode, data = child.stat().st_ino, child.read_bytes()
        for _ in range(256):
            child.unlink()
            child.write_bytes(data)
            if child.stat().st_ino == original_inode:
                break
        else:
            self.skipTest("fixture filesystem did not reuse the original flat-file inode")
        self.assertIn("identity changed", self.recover(plan, expected=2))
        self.assertEqual(child.read_bytes(), data)

    def test_addition_apply_records_real_git_calls_without_fetch_prune_or_reset(self):
        self.source_additions()
        real_git = shutil.which("git")
        launcher = self.root.parent / "trace-bin/git"
        launcher.parent.mkdir()
        trace = self.root.parent / "git-calls.jsonl"
        launcher.write_text(
            f"#!{sys.executable}\nimport json,os,sys\n"
            f"with open({str(trace)!r},'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n"
            f"os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n"
        )
        launcher.chmod(0o755)
        self.env["PATH"] = str(launcher.parent) + os.pathsep + self.env["PATH"]
        plan = self.cli()
        self.assertEqual(self.apply(plan)["status"], "completed")
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any("update-ref" in call for call in calls))
        self.assertTrue(any("update-index" in call for call in calls))
        self.assertFalse(any(any(word in call for word in ("fetch", "--prune", "reset", "push")) for call in calls))

    def test_large_source_blob_is_refused_before_effects(self):
        self.source_additions(payload="x" * 129)
        before = self.content_snapshot()
        self.assertIn("source blob exceeds", self.cli(
            expected=2, module_setup="syncwheel_local_replay.MAX_BYTES=128"))
        self.assertEqual(self.content_snapshot(), before)

    def test_total_source_delta_is_bounded_before_effects(self):
        self.source_additions(payload="x" * 128)
        before = self.content_snapshot()
        self.assertIn("source delta exceeds", self.cli(
            expected=2, module_setup="syncwheel_local_replay.MAX_BYTES=128"))
        self.assertEqual(self.content_snapshot(), before)

    def test_drift_in_source_tip_and_index_is_refused(self):
        plan = self.cli()
        self.git("update-index", "--assume-unchanged", "feature.txt")
        self.assertIn("hidden entries", self.apply(plan, expected=2))
        self.git("update-index", "--no-assume-unchanged", "feature.txt")
        self.git("update-ref", "refs/heads/feature", self.base, self.source)
        self.assertIn("exact declared stack tip", self.apply(plan, expected=2))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)

    def test_competing_git_writers_are_blocked_while_prepared(self):
        plan = self.cli()
        body = """if phase == 'ref-transaction-prepared':
 for argv in (['symbolic-ref','HEAD','refs/heads/feature'], ['update-ref','refs/heads/feature', 'HEAD'], ['add','feature.txt']):
  attempt = subprocess.run(['git',*argv],capture_output=True)
  assert attempt.returncode != 0, (argv, attempt.stderr)
"""
        self.assertEqual(self.apply(plan, checkpoint_code=body)["status"], "completed")

    def test_recovers_owned_checkout_after_crash_before_cas(self):
        plan = self.cli()
        self.apply(plan, crash="checkout-file-aligned", expected=87)
        self.wait_ref_locks()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertTrue((self.root / ".git/index.lock").exists())
        # Recovery does not re-run the source or claim its current ref.
        self.git("update-ref", "refs/heads/feature", self.base, self.source)
        self.assertEqual(self.recover(plan)["status"], "aborted")
        self.assertEqual((self.root / "feature.txt").read_text(), "old feature\n")
        self.assertEqual((self.root / "detail.txt").read_text(), "old detail\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".git/index.lock").exists())

    def test_recovers_committed_ref_without_replay(self):
        plan = self.cli()
        self.apply(plan, crash="ref-committed", expected=87)
        self.wait_ref_locks()
        new = self.git("rev-parse", "HEAD")
        self.assertNotEqual(new, self.old)
        self.assertEqual(self.recover(plan)["after"], new)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".git/index.lock").exists())

    def test_crash_recovery_preserves_unowned_bytes_and_locks(self):
        plan = self.cli()
        self.apply(plan, crash="index-aligned", expected=87)
        self.wait_ref_locks()
        lock = self.root / ".git/refs/heads/integration.lock"
        lock.write_bytes(b"foreign lock")
        self.assertIn("unowned Git ref lock", self.recover(plan, expected=2))
        self.assertEqual(lock.read_bytes(), b"foreign lock")
        lock.unlink()
        self.file("feature.txt", "unowned write\n")
        self.assertIn("staged bytes changed", self.recover(plan, expected=2))
        self.assertEqual((self.root / "feature.txt").read_text(), "unowned write\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)

    def test_pre_alignment_ref_advance_does_not_strand_our_index_lock(self):
        plan = self.cli()
        old_index = (self.root / ".git/index").read_bytes()
        old_manifest = (self.root / ".syncwheel/manifest.json").read_bytes()
        code = """if phase == 'index-lock-owned':
 old = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
 tree = subprocess.check_output(['git','rev-parse','HEAD^{tree}'], text=True).strip()
 new = subprocess.check_output(['git','commit-tree',tree,'-p',old,'-m','Competing advance'], text=True).strip()
 subprocess.run(['git','update-ref','refs/heads/integration',new,old],check=True)
"""
        self.assertIn("CAS lease", self.apply(plan, expected=2, checkpoint_code=code))
        advanced = self.git("rev-parse", "HEAD")
        self.assertNotEqual(advanced, self.old)
        lock = self.root / ".git/index.lock"
        self.assertTrue(lock.exists())
        self.assertEqual(self.recover(plan)["status"], "aborted")
        self.assertIsNone(self.recover(plan)["after"])
        self.assertFalse(lock.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), advanced)
        self.assertEqual((self.root / ".git/index").read_bytes(), old_index)
        self.assertEqual((self.root / ".syncwheel/manifest.json").read_bytes(), old_manifest)

    def test_refuses_symbolic_object_store_before_any_object_write(self):
        plan = self.cli()
        objects = self.root / ".git/objects"
        external = self.root.parent / "external-objects"
        objects.rename(external)
        objects.symlink_to(external, target_is_directory=True)
        entries = {str(path.relative_to(external)): path.read_bytes()
                   for path in external.rglob("*") if path.is_file()}
        self.assertIn("symbolic or external object store", self.cli(expected=2))
        self.assertIn("symbolic or external object store", self.apply(plan, expected=2))
        self.assertEqual({str(path.relative_to(external)): path.read_bytes()
                          for path in external.rglob("*") if path.is_file()}, entries)
        self.assertFalse((self.root / ".git/syncwheel-local-replay").exists())

    def test_refuses_empty_subtree_before_checkout(self):
        plan = self.cli()
        empty = subprocess.check_output(["git", "mktree"], cwd=self.root, input=b"",
                                        env=self.env).decode().strip()
        listing = self.git("ls-tree", "HEAD") + f"\n040000 tree {empty}\tempty\n"
        tree = subprocess.check_output(["git", "mktree"], cwd=self.root, input=listing,
                                       text=True, env=self.env).strip()
        tip = self.git("commit-tree", tree, "-p", self.old, "-m", "Empty subtree")
        self.git("update-ref", "refs/heads/integration", tip, self.old)
        self.assertIn("empty subtrees", self.cli(expected=2))
        self.assertIn("empty subtrees", self.apply(plan, expected=2))
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertIn("empty", self.git("ls-tree", "HEAD"))

    def test_refuses_effective_filters_and_untracked_attributes(self):
        plan = self.cli()
        self.git("config", "filter.fixture.clean", "exit 71")
        self.assertIn("full SHA-1 files repository", self.apply(plan, expected=2))
        self.git("config", "--unset", "filter.fixture.clean")
        self.file(".gitattributes", "feature.txt text eol=crlf\n")
        self.assertIn("effective checkout attributes", self.apply(plan, expected=2))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertEqual((self.root / "feature.txt").read_text(), "old feature\n")
        self.assertFalse((self.root / ".git/syncwheel-local-replay").exists())

    def test_preview_scrubs_trace_before_first_git(self):
        trace = self.root.parent / "ambient-trace"
        self.env["GIT_TRACE"] = str(trace)
        self.assertEqual(self.cli()["kind"], "localIntegrationReplayPlan")
        self.assertFalse(trace.exists())

    def test_vendored_cli_requires_matching_companion_only_for_replay(self):
        vendor = self.root.parent / "vendor"
        vendor.mkdir()
        cli = shutil.copy2(CLI, vendor)
        command = [sys.executable, cli, "int", "replay-local", "feature", "--repo", str(self.root)]
        result = subprocess.run(command, env=self.env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("matching syncwheel_local_replay.py", result.stderr)
        shutil.copy2(CLI.with_name("syncwheel_local_replay.py"), vendor)
        result = subprocess.run(command, env=self.env, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"], "localIntegrationReplayPlan")

    def test_group_crash_during_prepare_preserves_native_lock(self):
        plan = self.cli()
        self.apply(plan, expected=-signal.SIGKILL, checkpoint_code=(
            "if phase == 'ref-transaction-prepared': os.killpg(os.getpgrp(), signal.SIGKILL)"
        ))
        lock = self.root / ".git/refs/heads/integration.lock"
        self.assertTrue(lock.exists())
        before = lock.read_bytes()
        self.assertIn("unowned Git ref lock", self.recover(plan, expected=2))
        self.assertEqual(lock.read_bytes(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)

    def test_direct_manifest_drift_after_alignment_prevents_cas(self):
        plan = self.cli()
        script = """if phase == 'index-aligned':
 Path('.syncwheel/manifest.json').write_text('foreign edit')
"""
        self.assertIn("checkout differs at .syncwheel/manifest.json", self.apply(
            plan, expected=2, checkpoint_code=script,
        ))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.old)
        self.assertEqual((self.root / ".syncwheel/manifest.json").read_text(), "foreign edit")
        self.assertIn("preserving the journal", self.recover(plan, expected=2))

    def test_managed_hook_authorization_is_consumed(self):
        launcher = self.root.parent / "bin" / "syncwheel"
        launcher.parent.mkdir()
        launcher.write_text(
            f"#!{sys.executable}\n"
            f"import os,sys\nos.execv(sys.executable,[sys.executable,{str(CLI)!r},*sys.argv[1:]])\n"
        )
        launcher.chmod(0o755)
        self.env["PATH"] = str(launcher.parent) + os.pathsep + self.env["PATH"]
        result = subprocess.run(
            [str(launcher), "hooks", "install", "--apply", "--repo", str(self.root)],
            cwd=self.root, env=self.env, text=True, capture_output=True, timeout=25,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = self.cli()
        self.assertIsNotNone(plan["referenceHook"])
        self.assertEqual(self.apply(plan)["status"], "completed")
        self.assertEqual(list((self.root / ".git/syncwheel/ref-auth").glob("[0-9a-f]*")), [])

    def test_unknown_hook_refused_before_apply(self):
        hook = self.root / ".git/hooks/reference-transaction"
        hook.write_text("#!/bin/sh\nexit 99\n")
        hook.chmod(0o755)
        self.assertIn("exact installed managed reference hook", self.cli(expected=2))
        self.assertFalse((self.root / ".git/syncwheel-local-replay").exists())


if __name__ == "__main__":
    unittest.main()
