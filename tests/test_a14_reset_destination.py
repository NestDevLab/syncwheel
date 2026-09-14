import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_coordination as module


MARKER = "A14_RESET_DESTINATION_TRACKED_EDIT"


def _checkout_snapshot(case, fixture):
    snapshot = case.a14_source_snapshot(fixture)
    snapshot.pop("integration")
    return snapshot


def test_a14_reset_destination():
    case = module.ActiveActiveCoordinationTest(methodName="runTest")
    case.setUp()
    try:
        fixture = case.a14_published_tip_reuse_fixture(
            "a14-reset-destination", integration_strategy="cherry-pick"
        )
        caller = fixture["follower"]
        syncwheel = fixture["module"]
        integration_branch = fixture["manifest"]["integration"]["branch"]
        target = case.tmp / "a14-reset-destination-integration"
        case.git(
            caller,
            "worktree",
            "add",
            "-q",
            str(target),
            integration_branch,
        )

        target_fixture = dict(fixture)
        target_fixture["follower"] = target
        target_fixture["manifest_path"] = target / fixture["manifest_path"].relative_to(caller)
        caller_branch = case.git(caller, "branch", "--show-current").stdout.strip()
        target_branch = case.git(target, "branch", "--show-current").stdout.strip()
        plan = syncwheel.plan_published_integration_tip_reuse(
            caller, fixture["manifest"], fixture["manifest_path"]
        )
        replay_ready = bool(
            isinstance(plan, dict)
            and plan.get("status") == "replay"
            and plan.get("replayProductPaths")
        )
        caller_before = _checkout_snapshot(case, fixture)
        remote_before = case.a14_remote_snapshot(fixture)
        original_find = syncwheel.find_worktree_for_branch
        injected = []
        tracked_readme = []
        ref_advanced = []

        def inject_into_actual_reset_target(*args, **kwargs):
            result = original_find(*args, **kwargs)
            if (
                replay_ready
                and not injected
                and result is not None
                and Path(result).resolve() == target.resolve()
                and syncwheel.ref_tip(caller, fixture["integration_ref"])
                != plan["publishedTip"]
            ):
                tracked_readme.append(
                    case.git(
                        target, "ls-files", "--error-unmatch", "README.md"
                    ).returncode
                    == 0
                )
                ref_advanced.append(True)
                (target / "README.md").write_text(
                    "concurrent tracked edit in actual reset destination\n"
                )
                injected.append(case.a14_source_snapshot(target_fixture))
            return result

        error = None
        returncode = None
        stdout = io.StringIO()
        stderr = io.StringIO()
        if replay_ready:
            parser = syncwheel.build_parser()
            args = parser.parse_args(
                [
                    "int",
                    "rebuild",
                    "--repo",
                    str(caller),
                    "--reason",
                    "exercise separate reset-destination lease",
                ]
            )
            args.dry_run = False
            with mock.patch.object(
                syncwheel,
                "find_worktree_for_branch",
                side_effect=inject_into_actual_reset_target,
            ):
                try:
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        returncode = args.func(args)
                except syncwheel.SyncwheelError as exc:
                    error = str(exc)

        target_after = case.a14_source_snapshot(target_fixture)
        observed = {
            "caller_on_other_branch": bool(caller_branch)
            and caller_branch != integration_branch,
            "caller_snapshot_preserved": _checkout_snapshot(case, fixture) == caller_before,
            "plan_status_replay": bool(isinstance(plan, dict) and plan.get("status") == "replay"),
            "reached_after_ref_advance": len(injected) == 1 and ref_advanced == [True],
            "refused": error is not None or returncode == 2,
            "remote_snapshot_preserved": case.a14_remote_snapshot(fixture) == remote_before,
            "replay_product_paths_nonempty": bool(
                isinstance(plan, dict) and plan.get("replayProductPaths")
            ),
            "target_is_separate": target.resolve() != caller.resolve(),
            "target_on_integration_branch": target_branch == integration_branch,
            "target_readme_is_tracked": tracked_readme == [True],
            "target_readme_preserved": bool(injected)
            and target_after["readme"] == injected[0]["readme"],
            "target_snapshot_preserved": bool(injected) and target_after == injected[0],
        }
        print(
            "A14_RESET_DESTINATION_OBSERVATION="
            + json.dumps(observed, sort_keys=True),
            flush=True,
        )
        case.assertEqual(
            observed,
            {key: True for key in observed},
            MARKER,
        )
    finally:
        case.tearDown()


def load_tests(loader, tests, pattern):
    return loader.suiteClass([unittest.FunctionTestCase(test_a14_reset_destination)])
