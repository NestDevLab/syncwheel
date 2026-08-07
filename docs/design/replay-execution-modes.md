# Syncwheel: replay execution modes

Status: proposal. Written against Syncwheel 0.23.0.

## Purpose

Make branch replay independent of the working tree, so that a full multi-PR
workflow can run from a single checkout, and make the result identical whichever
execution path produced it.

The target workflow is: stay on the integration branch, develop there, and let
several pull requests be built and pushed from the manifest without ever checking
any of them out. Today that workflow is possible in principle but materializes one
worktree per stack in practice.

## Problem

### The working tree is a hard dependency of replay

Rebuilding a branch is `git reset --hard <base>` followed by `git cherry-pick
<commits>` (`scripts/syncwheel.py:3970`). Both are porcelain commands and require a
working tree and an index. The primary checkout cannot be borrowed for it: it is
reserved for the integration branch, and `validate` fails with a primary worktree
branch mismatch otherwise (`scripts/syncwheel.py:1171`).

So every stack rebuild materializes a second working tree, and by default a
persistent one under the configured worktree root
(`resolve_stack_rebuild_location:3402`). That default conflates two different
things: a worktree as a *tool* for one rebuild, and a worktree as a *desk* where
someone develops on that branch. The first should not survive the command that
created it.

The consequences are documented in [post-merge housekeeping](housekeeping-after-merge.md):
worktrees accumulate, each holding a full copy of the tree, and forgotten ones
produce confusing failures later.

### Replay is not deterministic

A separate defect, independent of the working tree, and a prerequisite for
everything below. `git cherry-pick` preserves the author but stamps a fresh
committer identity and date, and Syncwheel neither pins them nor passes `--ff`
(`scripts/syncwheel.py:4025`; `with_git_identity:202` only supplies a fallback name
and email). Replaying the same commits onto the same base therefore yields
different SHAs every time, so a rebuild is never a no-op and the manifest drifts on
every cycle.

Measured on a three-commit chain, replaying onto the original base:

| Run | Resulting SHAs |
|---|---|
| original commits | `f0a7deea1165  5f30eacea0f1` |
| replay as implemented today | `474e6320d9f1  9dbbe352da15` |
| replay with committer metadata carried over | `f0a7deea1165  5f30eacea0f1` |

Carrying `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` and `GIT_COMMITTER_DATE` from
each source commit, and cherry-picking one commit per invocation instead of the
current single batched call, is sufficient to make replay reproducible.

Two further leaks must be closed at the same time, or reproducibility holds only on
a clean machine:

- **`rerere`** auto-resolves conflicts from `.git/rr-cache`, which is per-clone. Replay would then
  depend on local state no manifest records — and `plumbing`, which has no rerere, would be
  inequivalent to the worktree modes on any conflicting commit.
- **`commit.gpgsign`** makes a replayed SHA unreproducible outright: the signature is not carried,
  and a locally generated one is not byte-stable.

Disable both for replay commands through `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`
environment variables, so the argv shape is preserved.

**`merge-stacks` is deterministic as well.** With `integration.strategy: merge-stacks`, the replay
is `git merge --no-ff <stack-branch>` (`scripts/syncwheel.py:4026`) and has no replay source commit
of its own. Syncwheel derives the merge commit's author and committer metadata from the tip of the
merged stack, so unchanged stack refs and base produce the same integration SHA.

## Determinism is a prerequisite, not an optimization

Without it, *which execution mode ran* determines the resulting SHAs. Two machines with different git
versions, or one taking a conflict fallback, would then build divergent branches from an identical
manifest — and the manifest, the coordination state branch, and every lease are all expressed in
SHAs. That is a correctness failure, not a performance difference.

Be precise about what it gates, or a cheap change gets blocked behind an expensive one:

| Gated | Not gated |
|---|---|
| `plumbing` — `commit-tree` with an explicit date and `cherry-pick` with a fresh one differ by design | `ephemeral` — runs identical argv in a tmpdir, byte-equivalent to today by construction |
| The differential test — with fresh timestamps, one mode run twice already differs, so "all modes equal" is unsatisfiable | |
| `capture-integration` in [draft stacks](draft-stacks.md) | |

With it, the modes below are interchangeable by construction.

## Execution modes

| Mode | Applies when | Working tree | Role |
|---|---|---|---|
| `plumbing` | git ≥ 2.38 and the replay does not conflict | none | default for automated replay |
| `in-place` | the caller already stands on the target branch and it is clean | the caller's own | free when it applies |
| `ephemeral` | porcelain is required but no human is | temporary, removed on exit | compatibility fallback |
| `desk` | a conflict needs resolution, or the caller wants to work on the branch | persistent | deliberate, never implicit |

### `plumbing`

```bash
T=$(git merge-tree --write-tree --merge-base=<commit>^ <head> <commit>)
N=$(git commit-tree $T -p <head> -m "<message>")   # with author/committer carried over
git update-ref refs/heads/<branch> $N
```

`git merge-tree --write-tree` (git ≥ 2.38) performs a three-way merge entirely in
memory and writes only a tree object. Verified in a bare repository — one with no
working tree at all — producing SHAs identical to the source commits.

On conflict it exits `1` and prints the conflicted paths and their stages, without
touching the filesystem. Conflicts are therefore *detected* at zero cost, and a
working tree is materialized only once it is established that a human is needed.

### `desk`

The only mode that leaves something behind, and the only one that should be chosen
explicitly. Two legitimate reasons: resolving a conflict, and wanting to build,
run, or test a branch in isolation. Neither is a side effect of a routine rebuild.

## Selection

Default `auto`: take the cheapest applicable mode, in the order above.

Two rules constrain the descent:

- **A conflict never descends silently.** `plumbing` reports the conflicted paths
  and stops. Escalating to `desk` is a decision the caller makes, because it means
  someone is about to do manual work.
- **The chosen mode is recorded** in the plan output and in the ledger event. An
  invisible optimization is not diagnosable when a rebuild produces something
  unexpected.

Configuration, most specific wins:

1. CLI flag on the individual command;
2. repo setting, for an operator who wants one mode always;
3. manifest default, for a repository whose contributors should share one;
4. built-in `auto`.

An operator who dislikes worktrees sets `plumbing`. One who wants every rebuild
inspectable sets `desk`. Neither has to argue with the tool.

## Equivalence testing

The real cost of shipping several modes is not writing them, it is keeping them
equal. A differential test is mandatory: for each scenario in a fixed corpus,
assert that every applicable mode yields byte-identical refs.

The corpus must include at least: a linear chain; a chain replayed onto a moved
base; a commit whose integration form was conflict-resolved; a merge commit in the
range; an empty commit; a binary file; a rename; and a file mode change.

Without that test, the modes will silently diverge and the divergence will surface
as a corrupt integration branch, not as a failing build.

## What this enables

Once replay does not need a working tree, the entire lifecycle runs from one
checkout: develop on integration, attribute commits to stacks, build and push every
PR branch as a manifest operation. Worktrees stop being a routine artifact and
become what their name suggests — a place someone chose to work.

It also composes with [draft stacks](draft-stacks.md): a draft's rebuild is the same
replay, so drafts become free of working-tree cost without needing a branch model
of their own.

## Risks

- **A PR branch is never seen locally.** Verification happens on integration, which
  means the combination is tested and the individual layer is not. This is
  acceptable only where the forge runs CI per layer; otherwise an isolated layer
  can be pushed having never been built. Repositories without per-layer CI should
  keep a `desk` step before publication.
- **Behavioural differences from `cherry-pick`.** The plumbing path bypasses
  `rerere`, the `-x` trailer, and cherry-pick hooks. Signing is available through
  `git commit-tree -S`.
- **Version gate.** `--write-tree` requires git ≥ 2.38. The gate must be detected,
  not assumed, and must fall back to `ephemeral` rather than failing.
- **Determinism regressions are silent.** Once SHAs are stable, anything that
  reintroduces a fresh timestamp breaks idempotence without breaking any test that
  does not compare SHAs across two runs. The differential test must run the same
  scenario twice.

## Rollout

1. Carry committer metadata through replay; one cherry-pick per commit. Closes the
   manifest drift on its own, with no new modes and no configuration.
2. Differential test harness over the corpus, against the single existing mode.
3. `ephemeral` wired into the rebuild path; `desk` becomes opt-in rather than
   default.
4. `plumbing` behind the version gate, with conflict detection and reporting.
5. Mode selection and configuration surface.

Step 1 is worth doing whether or not the rest follows. Step 3 is not blocked by step 1 — see the
gating table above — but is kept in this order because the harness gives every later step its safety
net.
