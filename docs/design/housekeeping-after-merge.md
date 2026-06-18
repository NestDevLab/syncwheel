# Syncwheel: post-merge housekeeping

## Purpose

Add an idempotent housekeeping path so that when a stack's PR lands — including
squash/rebase merges that rewrite commit SHAs — Syncwheel closes the stack and
reaps its local branch, worktree, and stale backup branches automatically, driven
by a trigger that fires right after the merge. This removes the unbounded
accumulation of worktrees, branches, and backups that the current append-only
lifecycle produces.

## Problem

The current lifecycle only *adds*: `stack create` opens a branch (and often a
worktree); reconcile/realign drops timestamped `backup/*` branches; merges happen
on the forge (squash). Nothing reaps a stack's branch / worktree / backups when its
PR lands. Over a couple of days of normal multi-stack work a single managed repo
accumulated 9 worktrees and 7 backup branches, and merged stacks lingered as
"active" in the manifest.

Three concrete defects, all in `scripts/syncwheel.py`:

### Defect 1 — close detects merges by SHA only; squash/rebase merges are invisible

`command_stack_close` checks, for each stack commit, `git merge-base --is-ancestor
<sha> <base_ref>`. A squash or rebase merge rewrites the SHA, so the original
commit is never an ancestor of the base → `close` refuses without `--force`. Worse,
`reconcile` sees a manifest stack whose branch is missing and plans
`create_pr_branch` to **recreate** the just-merged branch. Net effect: merged
stacks never auto-close, and reconcile actively fights the cleanup.

### Defect 2 — close never removes the worktree

`command_stack_close --delete-branch` runs `git branch -d <branch>` but does not
remove the stack's worktree. If the worktree still exists, `git branch -d` fails
anyway (branch is checked out elsewhere). The `git worktree remove --force`
primitive already exists in the materialize helpers but is not wired into close, so
worktrees leak indefinitely — the dominant source of the "too many worktrees"
problem, especially across an integration-scheme change that orphans old worktrees.

### Defect 3 — backups have no retention

`backup_branch_command` creates `backup/<branch>-before-syncwheel-<ts>` (and a
`-before-final-align-<ts>` variant) on every reconcile/realign. Nothing ever prunes
them.

## Goals / Non-goals

**Goals:** idempotent close + reap of merged stacks (SHA *or* content); bounded
backups; safe under concurrent multi-agent / multi-machine use; never destroy
unmerged or uncommitted work.

**Non-goals:** deleting unmerged feature branches; reaping worktrees that hold
uncommitted or conflicted changes (skip + report); performing the forge-side PR
merge itself.

## Design

### Shared truth vs local reaping (the multi-machine split)

The manifest and ledger are git-tracked and pushed, so the *closed* state is
shared; worktrees and local branches are machine-local. Responsibility splits along
that line:

| State | Lives | Cleaned by |
|-------|-------|------------|
| Manifest + ledger (closed status) | Git, shared | CI / any machine, then pushed |
| Worktree + local branch | One machine | Only that machine's local housekeep |

This is the rule that keeps multi-agent safe: CI cannot (and must not) reach into a
machine's worktrees, and no machine may remove a worktree another machine is using.
The manifest is the contract; local housekeep enforces it per machine.

### Merge detection: by content, not only SHA

A stack counts as merged if **either**:

- (a) every stack commit is reachable from the target ref (today's per-SHA check), **or**
- (b) `git diff --quiet <target_ref> <branch>` — the branch tip carries no content
  the target lacks. This covers squash and rebase merges.

Detection must `git fetch` first and compare against the stack's own
`target_remote`/`target_branch` (e.g. `origin/main`), not the integration branch.

### New entrypoint: `syncwheel housekeep`

Idempotent. Reports a plan by default; mutates only with `--apply` (consistent with
`reconcile`). Steps:

1. `git fetch --all --prune`.
2. For each active stack, if merged (a or b): close it (reuse `command_stack_close`
   logic), then reap —
   - if the stack worktree is dirty or has conflicts → **skip reap**, keep the
     branch, and report the path (never destroy uncommitted work);
   - else `git worktree remove --force <wt>` then `git worktree prune`;
   - then delete the branch (`-d` for SHA-merged, `-D` for content-merged, since
     `-d` won't recognize a squash) — only after the worktree is gone.
3. Prune backups: keep the most recent `backup_retention_count` (default 2) and any
   newer than `backup_retention_hours` (default 48); delete older `backup/*`. Always
   keep at least the newest.
4. Remove orphaned worktrees: any worktree under the syncwheel worktree root whose
   branch is referenced by no active stack and is clean → remove; report dirty
   orphans instead.
5. Append a `housekeep` ledger event summarizing closed stacks, reaped worktrees,
   pruned backups, and skipped (dirty) items.

### Triggers (the "right after merge" part)

1. **Local `post-merge` git hook** — on `git pull`/merge into the base branch, run
   `syncwheel housekeep --apply`. This is the natural "the merge reached this
   machine" moment and reaps the local branch + worktree for the stack that just
   landed. Ship as `githooks/post-merge` and teach `self install-hooks` to install
   it into managed repos.
2. **CI job** on `push: main` (or `pull_request: closed` with `merged == true`):
   run `syncwheel reconcile --close-merged --json`, then commit and push the updated
   manifest + ledger. Keeps the shared truth current for every machine/agent. CI
   intentionally does not reap worktrees — those are per-machine.
3. Optional periodic sweep (cron / scheduled run) calling `housekeep` as a backstop.

### Manifest schema additions

New optional `housekeeping` block; absent means the safe defaults below:

```json
"housekeeping": {
  "backup_retention_count": 2,
  "backup_retention_hours": 48,
  "reap_worktrees": true,
  "close_merged_by_content": true
}
```

## CLI surface

- `syncwheel housekeep [-r REPO] [--apply] [--json] [--no-reap-worktrees] [--keep-backups N]`
- `syncwheel stack close`: add `--merged-by-content` (close when diff vs target is
  empty, without `--force`); make `--delete-branch` reap the worktree first (after a
  dirty check).
- `syncwheel reconcile`: add `--close-merged` so detection folds into the reconcile
  plan, and suppress `create_pr_branch` when a manifest branch is missing but its
  content is already in the target.

## Acceptance criteria

1. **Squash-merged stack** (branch diff vs `origin/main` empty): `housekeep` closes
   it without `--force`, removes worktree + branch, manifest validates `OK`, ledger
   gains `stack_closed` + `housekeep`. (This is exactly the case reconcile currently
   mis-plans as `create_pr_branch`.)
2. **Unmerged stack** with real unique commits: untouched.
3. **Dirty/conflicted worktree**: not reaped; reported as skipped; branch kept.
4. **Backup retention**: backups beyond the policy pruned; the most recent K kept;
   nothing pruned when count ≤ K.
5. **Orphaned clean worktree** (branch in no active stack): removed; dirty orphan
   reported, not removed.
6. **Idempotent**: a second `housekeep` run is a no-op.
7. **Reconcile**: no longer plans `create_pr_branch` for a missing branch whose
   content is already in the target.

## Implementation pointers (`scripts/syncwheel.py`)

- `command_stack_close` (~L2550): add the merge-by-content branch to the
  reachability gate; reap the worktree before deleting the branch.
- Factor `reap_worktree(repo_root, branch)` from the existing `git worktree remove
  --force` calls in the materialize helpers (~L2191, L2243), using
  `find_worktree_for_branch` (~L1606) and `ensure_clean_worktree` (~L940) for the
  dirty check.
- `backup_branch_name` / `backup_branch_command` (~L1158): add
  `prune_backups(repo_root, retention)` enumerating `backup/*` refs by committer
  date.
- `reconcile_actions` (~L3039) / `classify_stack_reconcile` (~L3203) /
  `command_reconcile` (~L3350): add the `--close-merged` path and suppress
  `create_pr_branch` for content-merged missing branches.
- New `command_housekeep` + subparser; ledger event type `housekeep` via
  `append_ledger_event`.
- `githooks/post-merge` + `self install-hooks` wiring for managed repos.
- Tests under `tests/` covering the 7 acceptance scenarios.

## Rollout

1. Land detection + `housekeep` + tests (no behavior change until invoked).
2. Add manifest defaults (safe, backward-compatible).
3. Wire the post-merge hook and the CI job in managed repos.
4. Optionally make `reconcile` close-merged by default once proven in practice.
