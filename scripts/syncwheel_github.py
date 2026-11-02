#!/usr/bin/env python3
"""Fixed GitHub adapter for Syncwheel's plan-first PR merge contract."""

import json
import os
import re
import subprocess
import sys


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT = 12000
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
METHODS = {"squash", "merge", "rebase"}

PR_FIELDS = (
    "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,"
    "headRepository,author,commits,reviews,reviewDecision,mergeable,"
    "mergeStateStatus,statusCheckRollup,mergeCommit,mergedAt,mergedBy"
)
PR_LIST_FIELDS = "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid"

THREAD_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){"
    "reviewThreads(first:100){nodes{isResolved isOutdated}pageInfo{hasNextPage}}"
    "}}}"
)


EMPTY_RULES_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    "nameWithOwner viewerPermission isPrivate "
    "branchProtectionRules(first:100){totalCount pageInfo{hasNextPage} nodes{pattern}} "
    "rulesets(first:100,includeParents:true){totalCount pageInfo{hasNextPage} nodes{id}}"
    "}}"
)
PLAN_LIMIT_MESSAGE = "Upgrade to GitHub Pro or make this repository public to enable this feature."


class AdapterError(RuntimeError):
    pass


def bounded(value):
    value = (value or "").strip()
    return value[:MAX_OUTPUT]


def validate_repository(value):
    if not isinstance(value, str) or not REPOSITORY_RE.fullmatch(value.strip()):
        raise AdapterError("repository must be an OWNER/REPO identifier")
    return value.strip()


def validate_branch(value, field="branch"):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdapterError(f"{field} must be a non-empty branch name")
    return value.strip()


def validate_number(value):
    if isinstance(value, bool):
        raise AdapterError("pull request number must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError("pull request number must be an integer") from exc
    if number <= 0:
        raise AdapterError("pull request number must be positive")
    return number


def run_gh(argv, cwd, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Run one of the statically assembled gh argv vectors."""
    try:
        result = subprocess.run(
            ["gh", *argv],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=dict(os.environ),
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdapterError("gh executable is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("gh command timed out") from exc
    if result.returncode != 0:
        raise AdapterError(bounded(result.stderr) or bounded(result.stdout) or "gh command failed")
    return result.stdout


def run_json(argv, cwd, *, optional_statuses=()):
    try:
        output = run_gh(argv, cwd)
    except AdapterError as exc:
        # The caller needs the HTTP status for endpoints where a 404 means that
        # no branch protection/ruleset exists.  gh does not expose it cleanly in
        # all versions, so optional endpoints are retried with --include.
        if optional_statuses:
            return {"ok": False, "status": None, "detail": str(exc)}
        raise
    try:
        return {"ok": True, "value": json.loads(output)}
    except json.JSONDecodeError as exc:
        raise AdapterError(f"gh returned non-JSON output: {bounded(output)}") from exc


def _split_repository(repository):
    owner, name = repository.split("/", 1)
    return owner, name


def _head_repository(pr):
    head = pr.get("headRepository")
    if isinstance(head, dict):
        return head.get("nameWithOwner") or head.get("fullName") or head.get("name")
    return None


def _commit_authors(pr):
    authors = []
    for item in pr.get("commits") or []:
        if not isinstance(item, dict):
            continue
        sha = item.get("oid") or item.get("sha")
        raw_authors = item.get("authors")
        if isinstance(raw_authors, list):
            commit_authors = [author for author in raw_authors if isinstance(author, dict)]
        else:
            author = item.get("author") or {}
            commit_authors = [author] if isinstance(author, dict) else []
        if not commit_authors:
            commit_authors = [{}]
        for author in commit_authors:
            user = author.get("user")
            if isinstance(user, dict):
                login = user.get("login")
                name = author.get("name") or user.get("name")
            else:
                login = author.get("login")
                name = author.get("name")
            authors.append({"sha": sha, "login": login, "name": name})
    return authors


def _checks(pr):
    normalized = []
    for item in pr.get("statusCheckRollup") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("context") or item.get("app", {}).get("name")
        status = item.get("status") or item.get("state")
        conclusion = item.get("conclusion")
        if not status and conclusion:
            status = conclusion
        normalized.append({
            "name": name,
            "status": status,
            "conclusion": conclusion,
            "required": False,
        })
    return normalized


def _review_summary(pr, threads):
    reviews = []
    for item in pr.get("reviews") or []:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        reviews.append({
            "login": author.get("login") if isinstance(author, dict) else None,
            "state": item.get("state"),
        })
    active_threads = [
        item for item in threads
        if isinstance(item, dict) and item.get("isResolved") is not True and item.get("isOutdated") is not True
    ]
    changes_requested = any(item.get("state") == "CHANGES_REQUESTED" for item in reviews)
    return {
        "decision": pr.get("reviewDecision"),
        "changesRequested": changes_requested,
        "threads": threads,
        "unresolvedThreads": active_threads,
        "reviews": reviews,
    }


def _optional_api(repo, path, cwd):
    try:
        result = subprocess.run(
            ["gh", "api", "--include", path], cwd=cwd, text=True, capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS, env=dict(os.environ), shell=False, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"status": -1, "value": None, "detail": str(exc)}
    if result.returncode != 0:
        status = None
        match = re.search(r"HTTP/\S+\s+(\d{3})", result.stdout or result.stderr or "")
        if match:
            status = int(match.group(1))
        return {"status": status or -1, "value": None, "detail": bounded(result.stderr) or bounded(result.stdout)}
    body = result.stdout
    if "\n\n" in body:
        body = body.rsplit("\n\n", 1)[1]
    status = 200
    match = re.search(r"HTTP/\S+\s+(\d{3})", result.stdout)
    if match:
        status = int(match.group(1))
    if status == 404:
        return {"status": 404, "value": None, "detail": None}
    try:
        return {"status": status, "value": json.loads(body), "detail": None}
    except json.JSONDecodeError as exc:
        return {"status": 200, "value": None, "detail": f"non-JSON response: {exc}"}


def _plan_limited(result):
    detail = result.get("detail")
    return (
        type(result.get("status")) is int and result["status"] == 403
        and result.get("value") is None
        and isinstance(detail, str)
        and re.fullmatch(
            r"(?:gh: )?" + re.escape(PLAN_LIMIT_MESSAGE) + r"(?: \(HTTP 403\))?",
            detail,
        ) is not None
    )


def _compatible_empty_rest(result, empty_value):
    if _plan_limited(result):
        return True
    if type(result.get("status")) is not int:
        return False
    if result["status"] == 404:
        return result.get("value") is None
    return (
        result["status"] == 200 and result.get("detail") is None
        and type(result.get("value")) is type(empty_value)
        and result["value"] == empty_value
    )


def _complete_empty_connection(value):
    return (
        isinstance(value, dict)
        and type(value.get("totalCount")) is int and value["totalCount"] == 0
        and isinstance(value.get("nodes"), list) and value["nodes"] == []
        and isinstance(value.get("pageInfo"), dict)
        and value["pageInfo"].get("hasNextPage") is False
    )


def _observe_rules(repository, repo_info, protection, rulesets, cwd):
    rules = {
        "branchProtection": protection.get("value"),
        "branchProtectionStatus": protection.get("status"),
        "branchProtectionError": protection.get("detail"),
        "rulesets": rulesets.get("value"),
        "rulesetsStatus": rulesets.get("status"),
        "rulesetsError": rulesets.get("detail"),
        "observationSource": "rest",
    }
    if not (_plan_limited(protection) or _plan_limited(rulesets)):
        return rules
    if not (
        repo_info.get("private") is True
        and isinstance(repo_info.get("full_name"), str)
        and repo_info["full_name"].casefold() == repository.casefold()
        and isinstance(repo_info.get("permissions"), dict)
        and repo_info["permissions"].get("admin") is True
        and _compatible_empty_rest(protection, {})
        and _compatible_empty_rest(rulesets, [])
    ):
        return rules
    owner, name = _split_repository(repository)
    try:
        graph = run_json([
            "api", "graphql", "-f", f"query={EMPTY_RULES_QUERY}",
            "-F", f"owner={owner}", "-F", f"name={name}",
        ], cwd)["value"]
    except AdapterError as exc:
        rules["emptyRulesEvidenceError"] = str(exc)
        return rules
    # Any GraphQL errors, including errors alongside data, invalidate the proof.
    if not isinstance(graph, dict) or "errors" in graph:
        return rules
    data = graph.get("data")
    evidence = data.get("repository") if isinstance(data, dict) else None
    if not (
        isinstance(evidence, dict)
        and isinstance(evidence.get("nameWithOwner"), str)
        and evidence["nameWithOwner"].casefold() == repository.casefold()
        and evidence.get("viewerPermission") == "ADMIN"
        and evidence.get("isPrivate") is True
        and _complete_empty_connection(evidence.get("branchProtectionRules"))
        and _complete_empty_connection(evidence.get("rulesets"))
    ):
        return rules
    # Preserve the actual REST failures separately; normalized status describes
    # the successful alternative observation, never a synthetic REST success.
    rules.update({
        "restObservation": {"branchProtection": protection, "rulesets": rulesets},
        "observationSource": "graphql-complete-empty",
        "emptyRulesEvidence": evidence,
        "branchProtection": {}, "branchProtectionStatus": 200,
        "branchProtectionError": None,
        "rulesets": [], "rulesetsStatus": 200, "rulesetsError": None,
    })
    return rules


def observe(request, cwd):
    repository = validate_repository(request.get("repository"))
    base_branch = validate_branch(request.get("baseBranch"), "baseBranch")
    head_branch = request.get("headBranch")
    if head_branch is not None:
        head_branch = validate_branch(head_branch, "headBranch")
    owner, name = _split_repository(repository)

    repo_result = run_json(["api", f"repos/{repository}"], cwd)
    identity_result = run_json(["api", "user"], cwd)
    repo_info = repo_result["value"]
    identity = identity_result["value"]
    number = request.get("pullRequestNumber")
    if number is None:
        if not head_branch:
            raise AdapterError("headBranch or pullRequestNumber is required")
        listed = run_json([
            "pr", "list", "--repo", repository, "--state", "open",
            "--head", head_branch, "--base", base_branch,
            "--limit", "100", "--json", PR_LIST_FIELDS,
        ], cwd)["value"]
        if not isinstance(listed, list):
            raise AdapterError("gh pr list returned an invalid list")
        if len(listed) != 1:
            return {
                "schemaVersion": SCHEMA_VERSION,
                "repository": repository,
                "identity": identity,
                "repositoryInfo": repo_info,
                "pullRequests": listed,
                "selection": {"count": len(listed)},
                "rules": {},
            }
        number = listed[0].get("number")
    number = validate_number(number)
    pr = run_json([
        "pr", "view", str(number), "--repo", repository,
        "--json", PR_FIELDS,
    ], cwd)["value"]

    thread_result = run_json([
        "api", "graphql", "-f", f"query={THREAD_QUERY}",
        "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}",
    ], cwd)
    graph = thread_result["value"]
    thread_page = (
        (((graph or {}).get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    ).get("reviewThreads") or {}
    threads = thread_page.get("nodes") or []
    threads_truncated = bool((thread_page.get("pageInfo") or {}).get("hasNextPage"))

    protection = _optional_api(repo=repository, path=f"repos/{repository}/branches/{base_branch}/protection", cwd=cwd)
    rulesets = _optional_api(repo=repository, path=f"repos/{repository}/rules/branches/{base_branch}", cwd=cwd)
    allow = {
        "squash": bool(repo_info.get("allow_squash_merge")),
        "merge": bool(repo_info.get("allow_merge_commit")),
        "rebase": bool(repo_info.get("allow_rebase_merge")),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "repository": repository,
        "identity": {
            "login": identity.get("login") if isinstance(identity, dict) else None,
            "id": identity.get("id") if isinstance(identity, dict) else None,
        },
        "repositoryInfo": {
            "permissions": repo_info.get("permissions") or {},
            "allowMergeMethods": allow,
            "defaultBranch": repo_info.get("default_branch"),
            "isPrivate": bool(repo_info.get("private")),
            "visibility": repo_info.get("visibility"),
        },
        "pr": {
            "number": pr.get("number", number),
            "url": pr.get("url"),
            "state": pr.get("state"),
            "isDraft": bool(pr.get("isDraft")),
            "headRefName": pr.get("headRefName"),
            "headRefOid": pr.get("headRefOid"),
            "baseRefName": pr.get("baseRefName"),
            "baseRefOid": pr.get("baseRefOid"),
            "headRepository": _head_repository(pr),
            "author": (pr.get("author") or {}).get("login") if isinstance(pr.get("author"), dict) else None,
            "commitAuthors": _commit_authors(pr),
            "review": _review_summary(pr, threads),
            "mergeable": pr.get("mergeable"),
            "mergeStateStatus": pr.get("mergeStateStatus"),
            "mergedAt": pr.get("mergedAt"),
            "mergedBy": (pr.get("mergedBy") or {}).get("login") if isinstance(pr.get("mergedBy"), dict) else None,
            "checks": _checks(pr),
            "mergeCommit": (pr.get("mergeCommit") or {}).get("oid") if isinstance(pr.get("mergeCommit"), dict) else None,
            "threadsTruncated": threads_truncated,
        },
        "rules": dict(
            _observe_rules(repository, repo_info, protection, rulesets, cwd),
            allowMergeMethods=allow,
        ),
    }


def merge(request, cwd):
    repository = validate_repository(request.get("repository"))
    number = validate_number(request.get("pullRequestNumber"))
    method = request.get("method")
    if method not in METHODS:
        raise AdapterError("method must be squash, merge, or rebase")
    head_sha = request.get("headSha")
    if not isinstance(head_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise AdapterError("headSha must be a full commit SHA")
    admin = request.get("admin") is True
    argv = ["pr", "merge", str(number), "--repo", repository, f"--{method}"]
    if admin:
        argv.append("--admin")
    argv.extend(["--match-head-commit", head_sha])
    try:
        result = subprocess.run(
            ["gh", *argv], cwd=cwd, text=True, capture_output=True,
            timeout=DEFAULT_TIMEOUT_SECONDS, env=dict(os.environ), shell=False, check=False,
        )
    except FileNotFoundError as exc:
        raise AdapterError("gh executable is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("gh merge command timed out") from exc
    return {
        "schemaVersion": SCHEMA_VERSION,
        "ok": result.returncode == 0,
        "argv": argv,
        "returncode": result.returncode,
        "stdout": bounded(result.stdout),
        "stderr": bounded(result.stderr),
    }


def handle(request, cwd):
    if not isinstance(request, dict):
        raise AdapterError("request must be an object")
    operation = request.get("operation")
    if operation == "observe":
        return {"schemaVersion": SCHEMA_VERSION, "ok": True, "observation": observe(request, cwd)}
    if operation == "merge":
        return merge(request, cwd)
    raise AdapterError("operation must be observe or merge")


def main():
    try:
        request = json.load(sys.stdin)
        response = handle(request, os.getcwd())
    except (AdapterError, OSError, json.JSONDecodeError) as exc:
        response = {"schemaVersion": SCHEMA_VERSION, "ok": False, "error": str(exc)[:MAX_OUTPUT]}
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
