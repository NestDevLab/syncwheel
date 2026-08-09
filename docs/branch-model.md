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

Worktrees are desks a person chooses for development or conflict resolution.
They are not artifacts of a routine replay: `stack rebuild` and `int rebuild`
leave nothing behind unless you ask them to.

The default mode, `auto`, picks the cheapest path that applies:

| Mode | Chosen when | Leaves behind |
|---|---|---|
| `in-place` | the target branch is the current one | nothing |
| `desk` | the target branch already has a worktree, or `--worktree` was passed | that worktree |
| `plumbing` | Git supports `merge-tree --write-tree` (2.38 or newer) | nothing |
| `ephemeral` | below that Git threshold | nothing |

`plumbing` builds the replayed objects without creating a working tree and
updates the branch only after the complete replay succeeds. `ephemeral`
cherry-picks in a detached temporary worktree and removes it before the command
returns, including when replay fails. When a mode does not apply, `auto`
descends to the next one rather than failing.

If plumbing detects a conflict, it reports the paths and stops; rerun explicitly
with `--replay-mode desk` to obtain a checkout for resolution. Plumbing requires
its target branch not to be checked out, so it cannot leave an existing desk out
of sync with the updated ref.

Pin a mode when the default is not what a repository or an operator wants, most
specific first: the `--replay-mode` flag, `replay_mode` in the repo-local
`.syncwheel/profile.local.json` (`syncwheel replay-mode <mode>`), then
`defaults.replay_mode` in the manifest. Every rebuild records the mode it used
in its ledger event, and `plan --json` names the mode a rebuild would take.

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
