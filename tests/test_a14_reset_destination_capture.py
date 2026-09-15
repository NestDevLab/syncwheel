import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_coordination as coordination
import test_a14_reset_destination_lease as destination_cases


MARKER = "A14_RESET_DESTINATION_CAPTURE_DIRT_WINDOW"


def _run_unallowed_dirt_case(case):
    (
        fixture,
        caller,
        syncwheel,
        _integration_branch,
        target,
        target_manifest,
        plan,
    ) = destination_cases._fixture(case, "a14-reset-capture-unallowed")
    remote_before = case.a14_remote_snapshot(fixture)
    clean_at_entry = not case.git(target, "status", "--porcelain").stdout
    original_capture = syncwheel.checkout_reset_destination_lease
    injected = []
    allowance = []

    def inject_after_successful_capture(*args, **kwargs):
        result = original_capture(*args, **kwargs)
        worktree = result.get("worktree_path")
        if worktree and Path(worktree).resolve() == target.resolve() and not injected:
            allowed_paths = set(kwargs.get("allowed_paths") or ())
            allowance.append(sorted(allowed_paths))
            (target / "README.md").write_text(
                "dirt inserted after reset-destination preflight\n"
            )
            injected.append(
                destination_cases._reset_sensitive_snapshot(
                    case, target, target_manifest
                )
            )
        return result

    error = None
    returncode = None
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(
        syncwheel,
        "checkout_reset_destination_lease",
        side_effect=inject_after_successful_capture,
    ):
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                returncode = destination_cases._invoke_int_rebuild(syncwheel, caller)
        except syncwheel.SyncwheelError as exc:
            error = str(exc)

    target_after = destination_cases._reset_sensitive_snapshot(
        case, target, target_manifest
    )
    return {
        "clean_at_preflight_entry": clean_at_entry,
        "injected_after_successful_preflight": len(injected) == 1,
        "readme_not_allowed": bool(allowance) and "README.md" not in allowance[0],
        "plan_status_replay": bool(
            isinstance(plan, dict) and plan.get("status") == "replay"
        ),
        "destination_specific_refusal": bool(
            error
            and "integration reset destination" in error
            and "refus" in error
        ),
        "remote_snapshot_preserved": case.a14_remote_snapshot(fixture)
        == remote_before,
        "target_dirt_preserved": bool(injected) and target_after == injected[0],
        "error": error,
        "returncode": returncode,
    }


def _run_allowed_manifest_case(case):
    (
        fixture,
        caller,
        syncwheel,
        integration_branch,
        target,
        target_manifest,
        plan,
    ) = destination_cases._fixture(case, "a14-reset-capture-allowed")
    remote_before = case.a14_remote_snapshot(fixture)
    clean_at_entry = not case.git(target, "status", "--porcelain").stdout
    original_capture = syncwheel.checkout_reset_destination_lease
    original_run = syncwheel.run
    injected = []
    allowance = []
    reset_paths = []

    captures = 0

    def inject_allowed_manifest_after_second_capture(*args, **kwargs):
        nonlocal captures
        result = original_capture(*args, **kwargs)
        worktree = result.get("worktree_path")
        if worktree and Path(worktree).resolve() == target.resolve():
            captures += 1
        if captures == 2 and not injected:
            allowed_paths = set(kwargs.get("allowed_paths") or ())
            allowance.append(sorted(allowed_paths))
            target_manifest.write_bytes(target_manifest.read_bytes() + b"\n")
            injected.append(
                destination_cases._reset_sensitive_snapshot(
                    case, target, target_manifest
                )
            )
        return result

    def intercept_reset(argv, *args, **kwargs):
        command = [str(value) for value in argv]
        cwd = kwargs.get("cwd")
        if (
            len(command) >= 5
            and command[:4] == ["git", "read-tree", "--reset", "-u"]
            and cwd is not None
            and Path(cwd).resolve() == target.resolve()
        ):
            reset_paths.append(str(Path(cwd).resolve()))
            raise destination_cases.ResetIntercept(cwd)
        return original_run(argv, *args, **kwargs)

    intercepted = None
    error = None
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(
        syncwheel,
        "checkout_reset_destination_lease",
        side_effect=inject_allowed_manifest_after_second_capture,
    ), mock.patch.object(syncwheel, "run", side_effect=intercept_reset):
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                destination_cases._invoke_int_rebuild(syncwheel, caller)
        except destination_cases.ResetIntercept as exc:
            intercepted = exc.path
        except syncwheel.SyncwheelError as exc:
            error = str(exc)

    target_after = destination_cases._reset_sensitive_snapshot(
        case, target, target_manifest
    )
    manifest_relative = target_manifest.relative_to(target).as_posix()
    return {
        "clean_at_preflight_entry": clean_at_entry,
        "allowed_manifest_injected": len(injected) == 1,
        "manifest_is_allowed": bool(allowance)
        and manifest_relative in allowance[0],
        "plan_status_replay": bool(
            isinstance(plan, dict) and plan.get("status") == "replay"
        ),
        "no_capture_refusal": error is None,
        "reset_reached_on_leased_target": intercepted == target.resolve()
        and reset_paths == [str(target.resolve())],
        "remote_snapshot_preserved": case.a14_remote_snapshot(fixture)
        == remote_before,
        "allowed_manifest_preserved_before_reset": bool(injected)
        and target_after == injected[0],
        "error": error,
        "reset_paths": reset_paths,
    }


def test_a14_reset_destination_capture_rejects_only_unallowed_dirt():
    case = coordination.ActiveActiveCoordinationTest(methodName="runTest")
    case.setUp()
    try:
        unallowed = _run_unallowed_dirt_case(case)
        allowed = _run_allowed_manifest_case(case)
        observed = {
            **{
                f"unallowed_{key}": value
                for key, value in unallowed.items()
                if key not in {"error", "returncode"}
            },
            **{
                f"allowed_{key}": value
                for key, value in allowed.items()
                if key not in {"error", "reset_paths"}
            },
        }
        print(
            "A14_RESET_DESTINATION_CAPTURE_OBSERVATION="
            + json.dumps(
                {
                    **observed,
                    "unallowed_error": unallowed["error"],
                    "unallowed_returncode": unallowed["returncode"],
                    "allowed_error": allowed["error"],
                    "allowed_reset_paths": allowed["reset_paths"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        case.assertEqual(observed, {key: True for key in observed}, MARKER)
    finally:
        case.tearDown()


def load_tests(loader, tests, pattern):
    return loader.suiteClass(
        [
            unittest.FunctionTestCase(
                test_a14_reset_destination_capture_rejects_only_unallowed_dirt
            )
        ]
    )
