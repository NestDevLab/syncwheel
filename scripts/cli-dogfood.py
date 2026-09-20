#!/usr/bin/env python3
"""Exercise installed CLIs in disposable directories; preserve logs and failures.

No mocks, internal imports, live fleet writes, GitHub writes, or cleanup.
Run each scenario as a separate CI job after installing the candidate CLI.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time


parser = argparse.ArgumentParser()
parser.set_defaults(scenario="syncwheel")
parser.add_argument("--agentwheel-command", default='["agentwheel"]',
                    help="JSON argv prefix, for example [\"node\",\"/path/dist/index.js\"]")
parser.add_argument("--timeout", type=float, default=45)
parser.add_argument("--evidence-dir", type=Path)
parser.add_argument("--syncwheel-command", default=json.dumps(["python3", str(Path(__file__).resolve().parent / "syncwheel.py")]))
args = parser.parse_args()
root = args.evidence_dir.resolve() if args.evidence_dir else Path(tempfile.mkdtemp(prefix=f"cli-dogfood-{args.scenario}-"))
root.mkdir(parents=True, exist_ok=True)
print(f"Evidence: {root}", flush=True)
sw = json.loads(args.syncwheel_command)


def run(command, cwd=root, *, required=True):
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True,
                                text=True, timeout=args.timeout)
        row = dict(command=command, cwd=str(cwd), rc=result.returncode,
                   stdout=result.stdout, stderr=result.stderr)
    except subprocess.TimeoutExpired as exc:
        row = dict(command=command, cwd=str(cwd), rc=124,
                   stdout=str(exc.stdout or ""), stderr=str(exc.stderr or ""))
    row["seconds"] = round(time.monotonic() - started, 3)
    with (root / "commands.jsonl").open("a") as stream:
        stream.write(json.dumps(row) + "\n")
    print(f"{row['seconds']:.3f}s rc={row['rc']} {' '.join(command)}", flush=True)
    if required and row["rc"]:
        raise RuntimeError(row["stderr"] or row["stdout"])
    return row


def git_identity(repo):
    run(["git", "config", "user.name", "CLI Dogfood"], repo)
    run(["git", "config", "user.email", "cli-dogfood@example.invalid"], repo)


def syncwheel():
    run(sw + ["--version"])
    remote, repo = root / "origin.git", root / "repo"
    run(["git", "init", "--bare", "-b", "main", str(remote)])
    run(["git", "clone", str(remote), str(repo)])
    git_identity(repo)
    (repo / "README.md").write_text("CLI dogfood repository\n")
    run(["git", "add", "README.md"], repo)
    run(["git", "commit", "-m", "Initial repository"], repo)
    run(["git", "push", "-u", "origin", "main"], repo)
    run(sw + ["init", "--syncwheel-tracking", "git-tracked",
         "--base-branch", "main", "--integration-branch", "main-integration"], repo)
    run(sw + ["repo", "tracking", "status"], repo)
    run(sw + ["stack", "create", "hello", "--draft"], repo)
    response = run(sw + ["worktree", "open", "hello-lane", "--into", "hello", "--json"], repo)
    lane = Path(json.loads(response["stdout"])["lane"]["path"])
    (lane / "hello.txt").write_text("hello from a real commit\n")
    run(["git", "add", "hello.txt"], lane)
    run(["git", "commit", "-m", "Add hello through governed lane"], lane)
    sha = run(["git", "rev-parse", "HEAD"], lane)["stdout"].strip()
    run(sw + ["stack", "add", "hello", sha], repo)
    run(sw + ["int", "rebuild", "--reason", "CLI dogfood integration"], repo)
    assert (repo / "hello.txt").read_text() == "hello from a real commit\n"
    run(sw + ["publish", "--json"], repo)
    run(sw + ["stack", "promote", "hello"], repo)
    run(sw + ["stack", "push", "hello"], repo)
    remote_text = run(["git", "--git-dir", str(remote), "show", "pr/hello:hello.txt"])["stdout"]
    assert remote_text == "hello from a real commit\n"
    run(sw + ["publish", "--json"], repo)
    clone = root / "second-clone"
    run(["git", "clone", "-b", "main-integration", str(remote), str(clone)])
    git_identity(clone)
    run(sw + ["repo", "tracking", "status"], clone)
    run(sw + ["hooks", "install", "--apply"], clone)
    run(sw + ["resume", "--json"], clone)
    run(sw + ["resume", "--apply", "--json"], clone)
    validation = json.loads(run(sw + ["validate", "--json"], clone)["stdout"])
    assert not validation["errors"]
    assert (clone / "hello.txt").read_text() == remote_text

syncwheel()
print("PASS", flush=True)
