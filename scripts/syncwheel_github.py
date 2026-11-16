#!/usr/bin/env python3
"""Fixed GitHub adapter for Syncwheel's plan-first PR merge contract."""

from collections import Counter
import json
import os
import re
import subprocess
import sys


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT = 12000
CHECK_OBSERVATION_PAGE_LIMIT = 10
CHECK_ATTEMPT_REQUEST_LIMIT = 8
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
METHODS = {"squash", "merge", "rebase"}

PR_FIELDS = (
    "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid,"
    "headRepository,author,commits,reviews,reviewDecision,mergeable,"
    "mergeStateStatus,statusCheckRollup,mergeCommit,mergedAt,mergedBy"
)
PR_LIST_FIELDS = "number,url,state,isDraft,headRefName,headRefOid,baseRefName,baseRefOid"

CHECK_CONTEXT_FIELDS = (
    "totalCount pageInfo{hasNextPage endCursor} nodes{__typename "
    "... on CheckRun{id name status conclusion detailsUrl} "
    "... on StatusContext{id context state}}"
)
THREAD_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){"
    "reviewThreads(first:100){nodes{isResolved isOutdated}pageInfo{hasNextPage}}"
    "commits(last:1){nodes{commit{oid statusCheckRollup{"
    "contexts(first:100){" + CHECK_CONTEXT_FIELDS + "}"
    "}}}}}}}"
)
CHECK_PAGE_QUERY = (
    "query($owner:String!,$name:String!,$head:GitObjectID!,$cursor:String!){"
    "repository(owner:$owner,name:$name){"
    "object(oid:$head){... on Commit{oid statusCheckRollup{"
    "contexts(first:100,after:$cursor){" + CHECK_CONTEXT_FIELDS + "}"
    "}}}}}"
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
        legacy = item.get("__typename") == "StatusContext" or (
            "context" in item and item.get("__typename") != "CheckRun"
        )
        app = item.get("app")
        status = item.get("status") or item.get("state")
        conclusion = item.get("conclusion")
        check = {
            "name": item.get("name") or item.get("context"),
            "status": status or conclusion,
            "conclusion": conclusion,
            "required": False,
            "kind": "status-context" if legacy else "check-run",
        }
        if isinstance(app, dict) and _positive_integer(app.get("id")):
            check["appId"] = app["id"]
        if isinstance(item.get("headSha"), str):
            check["headSha"] = item["headSha"]
        if isinstance(item.get("detailsUrl"), str) and item["detailsUrl"]:
            check["detailsUrl"] = item["detailsUrl"]
        normalized.append(check)
    return normalized


def _positive_integer(value):
    return type(value) is int and value > 0


def _paged_objects(path, key, cwd, *, max_pages=CHECK_OBSERVATION_PAGE_LIMIT):
    rows = []
    total = None
    delimiter = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        endpoint = f"{path}{delimiter}per_page=100&page={page}"
        response = run_json(["api", endpoint], cwd)["value"]
        if not isinstance(response, dict):
            raise AdapterError(f"incomplete GitHub {key} observation")
        batch = response.get(key)
        count = response.get("total_count")
        if (type(count) is not int or count < 0 or count > max_pages * 100
                or (total is not None and count != total)
                or not isinstance(batch, list)
                or len(batch) != min(100, count - len(rows))
                or any(not isinstance(item, dict) for item in batch)):
            raise AdapterError(f"incomplete GitHub {key} observation")
        total = count
        rows.extend(batch)
        if len(rows) == total:
            return rows
    raise AdapterError(f"GitHub {key} observation exceeded its page limit")


def _check_attempts(repository, head, checks, runs, cwd):
    duplicates = {}
    for check in checks:
        if "runId" not in check:
            continue
        key = (check["runId"], check["name"])
        duplicates.setdefault(key, []).append(check)
    unresolved_runs = sorted({key[0] for key, rows in duplicates.items() if len(rows) > 1})
    remaining = CHECK_ATTEMPT_REQUEST_LIMIT
    incomplete = False
    base = f"repos/{repository}/actions/runs"
    expected_url = re.compile(
        rf"https://[^/]+/repos/{re.escape(repository)}/check-runs/([1-9][0-9]*)", re.IGNORECASE,
    )
    for run_id in unresolved_runs:
        last = runs[run_id].get("run_attempt")
        if not _positive_integer(last) or last > remaining:
            incomplete = True
            continue
        observed = {}
        remaining -= last
        try:
            for attempt in range(1, last + 1):
                jobs = _paged_objects(f"{base}/{run_id}/attempts/{attempt}/jobs", "jobs", cwd, max_pages=1)
                for job in jobs:
                    url = job.get("check_run_url")
                    match = expected_url.fullmatch(url) if isinstance(url, str) else None
                    if (not match or job.get("run_id") != run_id or job.get("head_sha") != head
                            or not isinstance(job.get("name"), str)):
                        raise AdapterError("a workflow job did not match its observed attempt")
                    check_id = int(match[1])
                    if check_id in observed:
                        raise AdapterError("a workflow job appeared in multiple attempts")
                    observed[check_id] = (attempt, job["name"])
        except AdapterError:
            incomplete = True
            continue
        for key, rows in duplicates.items():
            if key[0] != run_id:
                continue
            for row in rows:
                attempt = observed.get(row["checkRunId"])
                if attempt and attempt[1] == row["name"]:
                    row["runAttempt"] = attempt[0]
    return incomplete


def _graph_repository(graph):
    if not isinstance(graph, dict) or graph.get("errors"):
        return {}
    data = graph.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    return repository if isinstance(repository, dict) else {}


def _graph_check_nodes(graph, head, repository, cwd):
    pr = _graph_repository(graph).get("pullRequest")
    commits = pr.get("commits") if isinstance(pr, dict) else None
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        return None
    commit = nodes[0].get("commit")
    owner, name = _split_repository(repository)
    observed = []
    cursors = set()
    ids = set()
    total = None
    for page_number in range(CHECK_OBSERVATION_PAGE_LIMIT):
        if not isinstance(commit, dict) or commit.get("oid") != head:
            return None
        rollup = commit.get("statusCheckRollup")
        if rollup is None and "statusCheckRollup" in commit and page_number == 0:
            return []
        contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
        if not isinstance(contexts, dict):
            return None
        rows, page, count = (contexts.get(key) for key in ("nodes", "pageInfo", "totalCount"))
        if (not isinstance(rows, list) or len(rows) > 100 or type(count) is not int
                or not 0 <= count <= CHECK_OBSERVATION_PAGE_LIMIT * 100
                or (total is not None and total != count) or len(observed) + len(rows) > count
                or not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool):
            return None
        total = count
        for row in rows:
            if not isinstance(row, dict) or row.get("__typename") not in {"CheckRun", "StatusContext"}:
                return None
            node_id = row.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in ids:
                return None
            ids.add(node_id)
            observed.append(row)
        if not page["hasNextPage"]:
            return observed if len(observed) == total else None
        cursor = page.get("endCursor")
        if (not rows or len(observed) >= total or not isinstance(cursor, str) or not cursor
                or cursor in cursors or page_number + 1 == CHECK_OBSERVATION_PAGE_LIMIT):
            return None
        cursors.add(cursor)
        try:
            result = run_json([
                "api", "graphql", "-f", f"query={CHECK_PAGE_QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"head={head}", "-f", f"cursor={cursor}",
            ], cwd)["value"]
        except AdapterError:
            return None
        commit = _graph_repository(result).get("object")
    return None


def _observed_checks(pr, graph, repository, cwd):
    rollup = _checks(pr)
    check_runs = [item for item in rollup if item["kind"] == "check-run"]
    head = pr.get("headRefOid")
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        return rollup, bool(check_runs)
    graph_rows = _graph_check_nodes(graph, head, repository, cwd)
    base = f"repos/{repository}"
    try:
        raw_checks = _paged_objects(f"{base}/commits/{head}/check-runs?filter=all", "check_runs", cwd)
        ids = [item.get("id") for item in raw_checks]
        if (not all(_positive_integer(check_id) for check_id in ids)
                or len(set(ids)) != len(ids)
                or any(item.get("head_sha") != head for item in raw_checks)):
            raise AdapterError("check runs did not match the observed PR head")
    except AdapterError:
        return rollup, True

    actions = [
        item for item in raw_checks
        if isinstance(item.get("app"), dict) and item["app"].get("slug") == "github-actions"
    ]
    by_suite = {}
    actions_runs = {}
    incomplete = graph_rows is None
    if actions:
        try:
            runs = _paged_objects(f"{base}/actions/runs?head_sha={head}", "workflow_runs", cwd)
            keys = ("id", "check_suite_id", "workflow_id", "run_number")
            if (len(runs) >= 1000 or any(run.get("head_sha") != head
                    or not all(_positive_integer(run.get(key)) for key in keys) for run in runs)):
                raise AdapterError("workflow run history is incomplete or mismatches the PR head")
            suite_counts = Counter(run["check_suite_id"] for run in runs)
            run_counts = Counter(run["id"] for run in runs)
            for run in runs:
                suite = run["check_suite_id"]
                if suite_counts[suite] == run_counts[run["id"]] == 1:
                    by_suite[suite] = run
                    actions_runs[run["id"]] = run
                else:
                    incomplete = True
        except AdapterError:
            incomplete = True

    checks = []
    for item in raw_checks:
        app = item.get("app")
        suite = item.get("check_suite")
        status = item.get("status")
        conclusion = item.get("conclusion")
        check = {
            "kind": "check-run", "name": item.get("name"), "required": False,
            "status": status.upper() if isinstance(status, str) else None,
            "conclusion": conclusion.upper() if isinstance(conclusion, str) else None,
            "headSha": head, "checkRunId": item["id"],
        }
        if isinstance(item.get("details_url"), str) and item["details_url"]:
            check["detailsUrl"] = item["details_url"]
        if isinstance(item.get("node_id"), str) and item["node_id"]:
            check["checkNodeId"] = item["node_id"]
        if isinstance(app, dict) and _positive_integer(app.get("id")):
            check["appId"] = app["id"]
            if app.get("slug") == "github-actions" and isinstance(suite, dict):
                run = by_suite.get(suite.get("id")) if _positive_integer(suite.get("id")) else None
                if run and run["id"] in actions_runs:
                    check.update(
                        workflowId=run["workflow_id"], runId=run["id"],
                        runNumber=run["run_number"], runCreatedAt=run.get("created_at"),
                    )
        checks.append(check)

    attempts_incomplete = _check_attempts(repository, head, checks, actions_runs, cwd)
    # A disagreement between surfaces must remain visible. Only an exact
    # identity and outcome match can replace the original rollup row.
    def identity(check):
        name, url = check.get("name"), check.get("detailsUrl")
        if (not isinstance(name, str) or not name or not isinstance(url, str) or not url
                or check.get("headSha", head) != head):
            return None
        return name, url

    def outcome(check):
        return tuple(str(check.get(key) or "").upper() for key in ("status", "conclusion"))

    graph_by_identity = {}
    for row in graph_rows or ():
        if row.get("__typename") != "CheckRun" or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        key = identity(row)
        if key is not None:
            graph_by_identity.setdefault(key, []).append(row)
    rollup_counts = Counter(identity(check) for check in check_runs)
    rest_by_node = {}
    for check in checks:
        if "checkNodeId" in check:
            rest_by_node.setdefault(check["checkNodeId"], []).append(check)
    unmatched = []
    for check in check_runs:
        key = identity(check)
        graph_matches = graph_by_identity.get(key, [])
        rest_matches = rest_by_node.get(graph_matches[0]["id"], []) if len(graph_matches) == 1 else []
        if (key is None or rollup_counts[key] != 1 or len(rest_matches) != 1
                or identity(rest_matches[0]) != key or outcome(graph_matches[0]) != outcome(check)
                or outcome(rest_matches[0]) != outcome(check)
                or ("appId" in check and check["appId"] != rest_matches[0].get("appId"))):
            unmatched.append(check)
    seen_graph = set()
    for row in graph_rows or ():
        if row.get("__typename") != "CheckRun":
            continue
        node = row.get("id")
        if not isinstance(node, str) or not node or node in seen_graph:
            incomplete = True
            continue
        seen_graph.add(node)
        matching = rest_by_node.get(node, [])
        if len(matching) != 1 or identity(matching[0]) != identity(row) or outcome(matching[0]) != outcome(row):
            unmatched.extend(_checks({"statusCheckRollup": [{**row, "headSha": head}]}))
    contexts = [item for item in rollup if item["kind"] == "status-context"]
    for row in graph_rows or ():
        if row["__typename"] != "StatusContext":
            continue
        if not isinstance(row.get("context"), str) or not row["context"] or not isinstance(row.get("state"), str):
            incomplete = True
            continue
        observed_context, = _checks({"statusCheckRollup": [{**row, "headSha": head}]})
        if not any(item["name"] == observed_context["name"] and item.get("headSha", head) == head
                   and outcome(item) == outcome(observed_context) for item in contexts):
            contexts.append(observed_context)
    return [*contexts, *checks, *unmatched], incomplete or attempts_incomplete


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
    checks, checks_incomplete = _observed_checks(pr, graph, repository, cwd)
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
            "checks": checks,
            "checksIncomplete": checks_incomplete,
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
