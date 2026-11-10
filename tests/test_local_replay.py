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
        self.git("init", "-q", "-b", "main")
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

    def cli(self, *args, expected=0, crash=None, checkpoint_code=None):
        if crash or checkpoint_code:
            source = checkpoint_code or f"if phase == {crash!r}: os._exit(87)"
            runner = (
                "import os,sys,subprocess,signal; from pathlib import Path; "
                "sys.path.insert(0,sys.argv.pop(1)); import syncwheel,syncwheel_local_replay;\n"
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
