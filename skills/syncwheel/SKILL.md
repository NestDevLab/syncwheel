---
name: syncwheel
description: Use Syncwheel for deterministic, multi-agent-safe Git maintenance — PR branches, dedicated worktrees, stacked PRs, and integration branches. Use whenever you are about to create a PR branch, manage a fork/upstream/integration or PR-stack workflow, prepare or rebuild a stacked PR, or coordinate Git work on a repo that other people or agents may touch concurrently. Also covers the decision of whether to commit the Syncwheel manifest (own repo) or keep it untracked (external contribution).
allowed-tools: [Bash]
---

# Syncwheel

Syncwheel makes Git maintenance **deterministic and safe when many agents touch the
same repositories**. Branch ownership, PR stacks, and the integration branch are
described in a manifest; Syncwheel reconciles Git to match it. Plain manual
`git`/worktree surgery is the exception path, used only when Syncwheel is
unavailable, blocked, or cannot express the needed recovery.

## When to use (Syncwheel-first)

Reach for Syncwheel **before** any of these, not after:

- editing or handing off work in a Git repository that is shared, managed by
  Syncwheel, or likely to be touched by multiple agents
- creating a PR branch or a stack of dependent PRs
- working in a repo that has a `.syncwheel/` manifest, or a
  fork/upstream/integration/PR-stack workflow
- preparing changes in a dedicated worktree
- rebuilding or pushing PR branches / integration
- coordinating with other agents who may rebase the same branches

If the task involves a Git repo that has shared coordination risk, prefer
Syncwheel even when the visible edit is small. A local file change is not a
complete handoff by itself: before final response, report the Git state, required
checks, and the commit/push/PR decision or the explicit reason delivery remains
local.

## The contract

The script owns: repo-state discovery, manifest validation, deterministic branch
and integration reconstruction. The agent owns: judgment, communication,
project-specific validation after a rebuild, and safe execution.

## Locate the CLI

Syncwheel is available as the PATH `syncwheel` command. Install it with:

```bash
uv tool install "git+https://github.com/NestDevLab/syncwheel"   # production
uv tool install --editable <local-clone>                          # development
syncwheel self update                                             # keep current
```

If the PATH binary is not available (legacy host or vendored install), fall back to the checkout pointer:

```bash
SW="python3 ${SYNCWHEEL_REPO:?set SYNCWHEEL_REPO to the syncwheel checkout}/scripts/syncwheel.py"
$SW --version
```

Always run Syncwheel **against the target repo**: pass `-r <repo-path-or-alias>`
or run from inside the target repo's worktree.

## Safe lifecycle (always dry-run first)

```bash
syncwheel status --fetch          # discover real Git state
syncwheel validate                # manifest vs Git
syncwheel plan --json             # deterministic action plan
syncwheel reconcile               # dry-run reconcile (no writes)
syncwheel reconcile --apply --worktree-root <path>   # apply, only after the plan is understood
syncwheel reconcile --apply --worktree-root <path> --push   # publish shared branches
syncwheel check                   # re-verify
```

Short aliases are preferred when they make commands easier to scan:

```bash
syncwheel status -f -j
syncwheel plan -j
syncwheel reconcile -a -W var/syncwheel
syncwheel reconcile -a -P -W var/syncwheel
syncwheel repo tracking set git-tracked -a
```

Never mutate branches from a dirty worktree. Prefer a dedicated worktree for
every rebuild. Use `--dry-run` on rebuild/push commands. If the manifest and Git
disagree, fix the manifest or call out the conflict — do not claim a repo is
aligned while integration and PR branches still differ.

> ⚠️ **Rebuilds can silently revert already-applied work.** A `stack rebuild` /
> `int rebuild` reconstructs the branch from the **manifest's commit projection,
> not from the branch's current remote tip**. If the manifest points at a
> pre-cleanup commit (or a range that misses a later fix), the rebuild force-pushes
> the branch back to that older state and the cleanup/fix **disappears** — a real
> regression mode (observed in practice: a cleaned-up file came back after a rebuild
> off a stale projection). **Always:** before rebuilding, update the manifest with
> `syncwheel stack set <id> <rev-or-range>` so the projection includes the latest commit;
> and after every rebuild/sync/publish, diff the rebuilt branch against the expected
> post-fix state to confirm earlier cleanups did not regress.

## Authoring a new PR stack

```bash
# 1. Ensure a manifest exists (see the tracking decision below)
syncwheel init                                  # shared manifest (.syncwheel/manifest.json)
# 2. Persist the repo tracking policy before branch/push/PR work
syncwheel repo tracking status
syncwheel repo tracking set git-tracked --apply # or local-only
# 3. Declare the stack
syncwheel stack create feature-a --branch pr/feature-a --base origin/main --include-in-integration
# 4. Author in a dedicated worktree (fresh work uses plain git worktree add)
git worktree add -b pr/feature-a ../repo-wt-feature-a origin/main
#    ... make and commit your changes in that worktree ...
# 5. Record the commits into the manifest, then validate and push
syncwheel stack set feature-a origin/main..HEAD
syncwheel validate && syncwheel plan --json
syncwheel stack push feature-a
```

## Decision: Syncwheel tracking policy

This is a repo-local Syncwheel policy, not a social guess. Before editing in,
branching, pushing, opening a PR for, recovering, or handing off a shared or
Syncwheel-managed Git repo, run:

```bash
syncwheel repo tracking status
```

If `syncwheel_tracking` is missing, stop before branch, push, PR, stack,
worktree, recovery work, or final handoff. Do not guess, do not default
silently, and do not continue with a provisional policy. Ask the maintainer/user
whether this repo should be `git-tracked` or `local-only`, then persist the
answer:

```bash
syncwheel repo tracking set git-tracked --apply
syncwheel repo tracking set local-only --apply
```

### `git-tracked` → commit the manifest

Commit `.syncwheel/manifest.json` (and `.syncwheel/manifests/README.md`). Keep
personal overlays (`*.local.json`, `profile.local.json`) gitignored.

Benefits:
- the stack/integration topology is **versioned and shared** — every agent that
  clones inherits the same deterministic plan
- reproducible across machines without out-of-band setup
- the manifest is the team's **coordination contract**

Use this when the repo wants Syncwheel itself tracked under Git. Syncwheel writes
a managed `.gitignore` block for local-only metadata and repo-local worktrees
under `var/syncwheel/`.

### `local-only` → keep Syncwheel untracked

Exclude `.syncwheel/` and `var/syncwheel/` via `.git/info/exclude` (local, does
not touch the committed `.gitignore`).

Benefits:
- you still get worktree isolation, stacks, deterministic reconcile, and the
  ledger
- you do **not** impose Syncwheel config on a maintainer who may not use it
- your PRs stay clean — only the real change is proposed
- coordination/recovery happens via the canonical remote + `resume`

Use `syncwheel repo tracking set ... --apply` to migrate between modes. The CLI
edits only Syncwheel-managed ignore blocks; if manual `.gitignore` entries would
hide `.syncwheel/manifest.json`, stop and ask for a repository decision.

## Handoff checklist

Before final response after touching a Git repo:

- run `git status --short --branch` and state whether the worktree is clean,
  intentionally dirty, or blocked
- run the repo's required validation, or name the exact validation not run and why
- for `git-tracked` repos, commit and push the scoped work unless the user or
  repository policy explicitly requires local-only delivery
- if the user requested local-only edits, list the dirty files and the next
  command/decision needed to finish delivery
- never treat unrelated dirty files as a reason to skip this checklist; isolate
  your own changes and report unrelated residue separately

> **Manifest self-reference rule:** treat manifest edits and Syncwheel-version
> bumps as control-plane metadata, not as normal stack-owned product commits.
> Keep them in an admin checkout or a dedicated maintenance PR that is excluded
> from `integration.stacks`.

## Multi-agent / multi-machine

A shared, committed manifest plus the append-only ledger is what lets many agents
coordinate deterministically. On a fresh machine or a new agent, recover shared
state with `syncwheel resume` instead of improvising branch ownership.

## More

See `docs/manifest-tracking.md` for the full tracking policy, `docs/ai-agents.md`
and `docs/agent-procedure.md` for the agent contract, and `docs/core-procedure.md`
for the canonical recovery procedure.
