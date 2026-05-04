# Manual Git Flow

This document explains the Git workflow that Syncwheel automates.

It is useful when you want to understand the model, audit what Syncwheel is
doing, or recover a case that is not yet expressible through the CLI. It is not
the preferred day-to-day workflow. The manual version is intentionally verbose
because every operation has to preserve commit ownership, branch publication
state, and integration history by hand.

## Core idea

Git stores branch history. It does not store PR ownership.

Syncwheel adds a manifest that declares:

- the canonical base ref, such as `origin/main`
- one logical stack per PR branch
- the exact commits owned by each stack
- the integration branch, if one is used
- the order in which stacks are projected into integration

With that declaration, PR branches and the integration branch become
rebuildable projections of the manifest. The manual flow below performs those
same projections with raw Git.

## Names used below

Replace these placeholders with your repository's real names:

```text
<base-ref>             origin/main
<remote>               origin
<stack-id>             feature-a
<stack-branch>         pr/feature-a
<integration-branch>   integration/project-stack
<stack-worktree>       ../wt-pr-feature-a
<integration-worktree> ../wt-integration
```

## Safety rules

Before any branch-moving operation:

```bash
git fetch <remote> --prune
git status --short --branch
git worktree list
```

Only continue when the affected worktree is clean. If the target branch already
exists and will be moved, create a backup first:

```bash
git branch backup/<branch-name>-before-manual-sync-$(date +%Y%m%d%H%M%S) <branch-name>
```

Do not run a normal `git pull` or merge on a managed PR branch or integration
branch as a repair step. These branches are projections. If they drift, rebuild
or align them deliberately.

## 1. Identify stack ownership manually

For each PR branch, list the commits that should belong to that stack:

```bash
git rev-list --reverse <base-ref>..<stack-branch>
```

Review the list:

```bash
git log --oneline --decorate --reverse <base-ref>..<stack-branch>
```

Conceptually, this becomes:

```json
{
  "id": "<stack-id>",
  "branch": "<stack-branch>",
  "commits": ["<sha-1>", "<sha-2>"]
}
```

The important rule is that each persistent product commit belongs to exactly
one stack. Temporary integration-only work should be named explicitly instead
of silently mixed into a PR branch.

## 2. Rebuild one PR branch manually

Create or refresh a dedicated worktree at the base ref:

```bash
git worktree add -B <stack-branch> <stack-worktree> <base-ref>
```

Replay the stack's declared commits in order:

```bash
git -C <stack-worktree> cherry-pick <sha-1> <sha-2>
```

If a conflict occurs, resolve it in the worktree, then continue:

```bash
git -C <stack-worktree> status
git -C <stack-worktree> add <resolved-files>
git -C <stack-worktree> cherry-pick --continue
```

Validate the resulting branch:

```bash
git -C <stack-worktree> log --oneline --decorate --reverse <base-ref>..HEAD
git -C <stack-worktree> status --short --branch
```

Publish the rewritten PR branch only when this replacement history is intended:

```bash
git -C <stack-worktree> push --force-with-lease <remote> HEAD:<stack-branch>
```

`--force-with-lease` is the normal manual equivalent of Syncwheel's managed
branch publish. It rejects the push if the remote moved since your last fetch.

## 3. Rebuild integration manually

Integration is a projection of ordered stacks. There are two common strategies.

For a linear integration history, start from the base and cherry-pick every
declared stack commit in manifest order:

```bash
git worktree add -B <integration-branch> <integration-worktree> <base-ref>
git -C <integration-worktree> cherry-pick <feature-a-sha-1> <feature-a-sha-2>
git -C <integration-worktree> cherry-pick <feature-b-sha-1> <feature-b-sha-2>
```

For merge-shaped integration history, rebuild the stack branches first, then
merge those branch tips in manifest order:

```bash
git worktree add -B <integration-branch> <integration-worktree> <base-ref>
git -C <integration-worktree> merge --no-ff <stack-branch-a>
git -C <integration-worktree> merge --no-ff <stack-branch-b>
```

Validate and publish:

```bash
git -C <integration-worktree> log --oneline --decorate <base-ref>..HEAD
git -C <integration-worktree> status --short --branch
git -C <integration-worktree> push --force-with-lease <remote> HEAD:<integration-branch>
```

## 4. Update commit SHAs after a rebuild

Cherry-picking onto a new base usually creates replacement commit SHAs.

After rebuilding a stack branch, inspect the new commits:

```bash
git -C <stack-worktree> rev-list --reverse <base-ref>..HEAD
```

The manifest must be updated to the new SHA list for that stack. Without this
step, the next projection will be based on stale historical commits.

## 5. Converge two devices manually

Start by fetching:

```bash
git fetch <remote> --prune
```

Compare local and remote branch refs:

```bash
git rev-parse <stack-branch>
git rev-parse <remote>/<stack-branch>
```

Compare their final trees:

```bash
git rev-parse <stack-branch>^{tree}
git rev-parse <remote>/<stack-branch>^{tree}
```

Then classify the case:

- If local and remote are the same ref, there is nothing to normalize.
- If the remote matches the manifest projection and the local branch does not,
  align local to remote after backing up the local branch.
- If local and remote both match the manifest projection but have different
  refs, align local to remote when you want Git status to stop showing
  ahead/behind.
- If neither side matches the manifest projection, rebuild from the manifest
  instead of merging local and remote.

Manual local-to-remote alignment:

```bash
git branch backup/<stack-branch>-before-manual-align-$(date +%Y%m%d%H%M%S) <stack-branch>
git switch <stack-branch>
git reset --hard <remote>/<stack-branch>
```

Use the same pattern for integration, but only after verifying that the remote
integration branch matches the manifest projection. Never use a plain merge to
"fix" a diverged managed integration branch.

## 6. What Syncwheel replaces

The manual lifecycle is:

1. fetch remotes
2. inspect local and remote branch tips
3. map commits to logical stacks
4. rebuild stack branches from the base
5. update the manifest to replacement SHAs
6. rebuild integration from stack order
7. publish replacement branch histories with `--force-with-lease`
8. align clean local histories to already-published valid remote histories

Syncwheel packages those steps as:

```bash
python3 scripts/syncwheel.py reconcile
python3 scripts/syncwheel.py reconcile --apply --worktree-root <path>
python3 scripts/syncwheel.py reconcile --apply --push --worktree-root <path>
python3 scripts/syncwheel.py reconcile --apply --align-local-to-remote
```

The manifest is the contract. Git branches are materialized views of that
contract.
