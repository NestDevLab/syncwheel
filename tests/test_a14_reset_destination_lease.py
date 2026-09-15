import contextlib
import inspect
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_coordination as module


PATH_MARKER = "A14_RESET_DESTINATION_SINGLE_OBSERVATION"
DIRT_MARKER = "A14_RESET_DESTINATION_DIRT_WINDOW"


class ResetIntercept(RuntimeError):
    def __init__(self, path):
        super().__init__(str(path))
        self.path = Path(path).resolve()


def _reset_sensitive_snapshot(case, repo, manifest_path):
    return {
        "readme": (repo / "README.md").read_bytes(),
        "index": case.git(repo, "ls-files", "--stage", "-z").stdout,
        "unstaged": case.git(repo, "diff-files", "--name-only", "-z").stdout,
        "untracked": case.git(
            repo, "ls-files", "--others", "--exclude-standard", "-z"
        ).stdout,
        "manifest": manifest_path.read_bytes(),
        "gitignore": (repo / ".gitignore").read_bytes(),
    }


def _fixture(case, name):
    fixture = case.a14_published_tip_reuse_fixture(
        name, integration_strategy="cherry-pick"
    )
    caller = fixture["follower"]
    syncwheel = fixture["module"]
    integration_branch = fixture["manifest"]["integration"]["branch"]
    target = case.tmp / f"{name}-integration"
    case.git(caller, "worktree", "add", "-q", str(target), integration_branch)
    target_manifest = target / fixture["manifest_path"].relative_to(caller)
    plan = syncwheel.plan_published_integration_tip_reuse(
        caller, fixture["manifest"], fixture["manifest_path"]
    )
    selected, selected_path = syncwheel.load_manifest(
        caller, fixture["manifest_path"]
    )
    selected["defaults"]["replay_mode"] = "auto"
    syncwheel.save_manifest(selected_path, selected)
    fixture["manifest"] = selected
    fixture["manifest_path"] = selected_path
    return fixture, caller, syncwheel, integration_branch, target, target_manifest, plan


def _invoke_int_rebuild(syncwheel, caller):
    parser = syncwheel.build_parser()
    args = parser.parse_args(
        [
            "int",
            "rebuild",
            "--repo",
            str(caller),
            "--reason",
            "exercise reset-destination lease",
        ]
    )
    args.dry_run = False
    return args.func(args)


def test_a14_refuses_when_reset_destination_changes_after_lease():
    case = module.ActiveActiveCoordinationTest(methodName="runTest")
    case.setUp()
    try:
        (
            fixture,
            caller,
            syncwheel,
            integration_branch,
            target,
            target_manifest,
            plan,
        ) = _fixture(case, "a14-reset-destination-path")
        decoy = case.tmp / "a14-reset-destination-decoy"
        decoy.mkdir()
        remote_before = case.a14_remote_snapshot(fixture)
        target_before = _reset_sensitive_snapshot(case, target, target_manifest)
        original_find = syncwheel.find_worktree_for_branch
        substituted = []

        def choose_worktree(*args, **kwargs):
            result = original_find(*args, **kwargs)
            callers = [frame.function for frame in inspect.stack()[1:]]
            ref_advanced = bool(
                isinstance(plan, dict)
                and syncwheel.ref_tip(caller, fixture["integration_ref"])
                != plan.get("publishedTip")
            )
            if (
                ref_advanced
                and "apply_integration_reconciliation" in callers
                and not substituted
            ):
                substituted.append(str(decoy.resolve()))
                return decoy
            return result

        error = None
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            syncwheel, "find_worktree_for_branch", side_effect=choose_worktree
        ):
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    _invoke_int_rebuild(syncwheel, caller)
            except syncwheel.SyncwheelError as exc:
                error = str(exc)

        pending = syncwheel.pending_control_manifest_intents(
            syncwheel.load_ledger_events(caller, fixture["manifest_path"])
        )
        observed = {
            "plan_status_replay": bool(
                isinstance(plan, dict) and plan.get("status") == "replay"
            ),
            "destination_change_injected": substituted == [str(decoy.resolve())],
            "destination_change_refused": bool(
                error and "integration reconciliation checkout moved" in error
            ),
            "pending_intent_preserved": len(pending) == 1,
            "remote_snapshot_preserved": case.a14_remote_snapshot(fixture)
            == remote_before,
            "target_branch_identity": case.git(
                target, "branch", "--show-current"
            ).stdout.strip()
            == integration_branch,
            "target_checkout_preserved": _reset_sensitive_snapshot(
                case, target, target_manifest
            ) == target_before,
        }
        print(
            "A14_RESET_DESTINATION_PATH_OBSERVATION="
            + json.dumps(
                {
                    **observed,
                    "error": error,
                    "substituted": substituted,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        case.assertEqual(observed, {key: True for key in observed}, PATH_MARKER)
    finally:
        case.tearDown()


def test_a14_refuses_dirt_added_after_reset_destination_preflight():
    case = module.ActiveActiveCoordinationTest(methodName="runTest")
    case.setUp()
    try:
        (
            fixture,
            caller,
            syncwheel,
            _integration_branch,
            target,
            target_manifest,
            plan,
        ) = _fixture(case, "a14-reset-destination-dirt")
        remote_before = case.a14_remote_snapshot(fixture)
        clean_at_entry = not case.git(target, "status", "--porcelain").stdout
        original_plan = syncwheel.plan_published_integration_tip_reuse
        injected = []

        def inject_after_preflight(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            if not injected:
                (target / "README.md").write_text(
                    "dirt added after reset-destination preflight\n"
                )
                injected.append(
                    _reset_sensitive_snapshot(case, target, target_manifest)
                )
            return result

        error = None
        returncode = None
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            syncwheel,
            "plan_published_integration_tip_reuse",
            side_effect=inject_after_preflight,
        ):
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    returncode = _invoke_int_rebuild(syncwheel, caller)
            except syncwheel.SyncwheelError as exc:
                error = str(exc)

        target_after = _reset_sensitive_snapshot(case, target, target_manifest)
        observed = {
            "clean_at_preflight_entry": clean_at_entry,
            "dirt_injected_after_preflight": len(injected) == 1,
            "plan_status_replay": bool(
                isinstance(plan, dict) and plan.get("status") == "replay"
            ),
            "refused": error is not None or returncode == 2,
            "remote_snapshot_preserved": case.a14_remote_snapshot(fixture)
            == remote_before,
            "target_dirt_preserved": bool(injected) and target_after == injected[0],
        }
        print(
            "A14_RESET_DESTINATION_DIRT_OBSERVATION="
            + json.dumps(
                {
                    **observed,
                    "error": error,
                    "returncode": returncode,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        case.assertEqual(observed, {key: True for key in observed}, DIRT_MARKER)
    finally:
        case.tearDown()


def load_tests(loader, tests, pattern):
    return loader.suiteClass(
        [
            unittest.FunctionTestCase(
                test_a14_refuses_when_reset_destination_changes_after_lease
            ),
            unittest.FunctionTestCase(
                test_a14_refuses_dirt_added_after_reset_destination_preflight
            ),
        ]
    )
