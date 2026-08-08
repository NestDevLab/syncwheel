# Syncwheel branch model

## Intent

Keep four concerns separate:
- canonical upstream history
- publication remote history
- integration/runtime history
- PR review surfaces

## Default model

Unless a repo documents otherwise:
- `main` or equivalent is the canonical base
- `main-integration` is where day-to-day combined work happens
- `pr/*` branches are extracted review surfaces for upstream PRs
- stacks are `published` by default; a `draft` stack is still an owned
  integration branch, but carries an explicit non-publication topology state
- draft source branches use `syncwheel/draft/<stack-id>` and are materialized
  like any other stack branch; published stacks normally use `pr/<stack-id>`
- integration should not be the only home of long-lived product changes

## Deterministic mapping

The important step is not just naming branches. It is declaring:
- which commits belong to which logical stack
- which stack maps to which `pr/*` branch
- whether that stack is `draft` or `published`
- in what order stacks are replayed into integration

Without that mapping, Git can only infer ownership heuristically.

With that mapping, syncwheel becomes scriptable.

## Draft lifecycle

A draft is never branchless. Create it with `stack create --draft`; the command
creates `syncwheel/draft/<stack-id>` immediately so validation and deterministic
rebuilds have a durable owner. Draft stacks remain in integration and can be
rebuilt, but cannot be pushed through `stack push` or `reconcile --push`.

`stack promote <stack>` changes a draft to published and renames the source to
its PR branch. `stack demote <stack>` changes published back to draft without a
rename and refuses a stack with `github.pr`. In active-active coordination, a
promotion publishes the new PR ref atomically, retains the old draft remote ref
as a tombstone, and leaves any old-name reconcile worktree directory in place
for explicit follow-up.

## Worktrees

Worktrees are desks a person chooses for development or conflict resolution;
they are not required artifacts of a routine replay. Use `--replay-mode
ephemeral` on `stack rebuild` or `int rebuild` to cherry-pick in a detached
temporary worktree. Syncwheel updates the real branch ref and removes that
worktree before the command returns, including when replay fails.

`auto` remains desk-compatible for this release, so use `ephemeral` explicitly
when no persistent checkout is wanted. `plumbing` is reserved for a later
release and currently reports that it is unavailable.

Recommended persistent layout:
- repo root = active integration checkout by default
- optional administrative checkout for manifest-only work
- optional worktree for an actively developed or manually repaired PR branch

## Safe defaults

- base PR branches from canonical main
- do normal development on integration
- make every persistent integration change also belong to a PR stack
- keep integration-specific glue visible and rare
- prefer declarative stack repair over ad-hoc rebases

## Visibility rule

If IDE UI and `git status` disagree with reality, trust the full graph plus the syncwheel manifest, not the branch badge.
