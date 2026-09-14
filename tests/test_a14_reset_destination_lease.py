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


def test_a14_reset_uses_the_final_observed_destination():
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
        target_readme_before = (target / "README.md").read_bytes()
        original_find = syncwheel.find_worktree_for_branch
        original_run = syncwheel.run
        direct_lookups = []
        observed_lookups = []
        reset_paths = []

        def choose_worktree(*args, **kwargs):
            result = original_find(*args, **kwargs)
            callers = [frame.function for frame in inspect.stack()[1:]]
            caller_name = next(
                (
                    name
                    for name in callers
                    if name
                    in {
                        "checkout_reset_destination_lease",
                        "execute_replay_steps",
                    }
                ),
                None,
            )
            ref_advanced = bool(
                isinstance(plan, dict)
                and syncwheel.ref_tip(caller, fixture["integration_ref"])
                != plan.get("publishedTip")
            )
            if ref_advanced and caller_name == "execute_replay_steps":
                direct_lookups.append(str(decoy.resolve()))
                return decoy
            if ref_advanced and caller_name == "checkout_reset_destination_lease":
                observed_lookups.append(str(Path(result).resolve()) if result else None)
            return result

        def intercept_reset(argv, *args, **kwargs):
            command = [str(value) for value in argv]
            if (
                len(command) >= 6
                and command[:2] == ["git", "-C"]
                and command[3:5] == ["reset", "--hard"]
                and command[5] == integration_branch
            ):
                reset_paths.append(str(Path(command[2]).resolve()))
                raise ResetIntercept(command[2])
            return original_run(argv, *args, **kwargs)

        intercepted = None
        error = None
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            syncwheel, "find_worktree_for_branch", side_effect=choose_worktree
        ), mock.patch.object(syncwheel, "run", side_effect=intercept_reset):
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    _invoke_int_rebuild(syncwheel, caller)
            except ResetIntercept as exc:
                intercepted = exc.path
            except syncwheel.SyncwheelError as exc:
                error = str(exc)

        observed = {
            "final_reset_was_intercepted": intercepted is not None,
            "plan_status_replay": bool(
                isinstance(plan, dict) and plan.get("status") == "replay"
            ),
            "remote_snapshot_preserved": case.a14_remote_snapshot(fixture)
            == remote_before,
            "reset_uses_actual_destination": reset_paths == [str(target.resolve())],
            "target_branch_identity": case.git(
                target, "branch", "--show-current"
            ).stdout.strip()
            == integration_branch,
            "target_readme_preserved_before_reset": (target / "README.md").read_bytes()
            == target_readme_before,
        }
        print(
            "A14_RESET_DESTINATION_PATH_OBSERVATION="
            + json.dumps(
                {
                    **observed,
                    "direct_lookups": direct_lookups,
                    "error": error,
                    "observed_lookups": observed_lookups,
                    "reset_paths": reset_paths,
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
                test_a14_reset_uses_the_final_observed_destination
            ),
            unittest.FunctionTestCase(
                test_a14_refuses_dirt_added_after_reset_destination_preflight
            ),
        ]
    )
