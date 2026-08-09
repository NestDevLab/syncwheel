# Syncwheel core procedure

## Goal

Bring a fork/upstream/integration repo back to a state that is simultaneously:
- truthful
- reviewable
- operational
- scriptable

## Phase 1. Recover the actual repo model

Always inspect:
- remotes and what they really mean
- canonical base branch
- integration branch
- whether the primary Git worktree is on that integration branch
- active `pr/*` branches
- worktrees
- stashes
- whether the repo root is an admin checkout or an active worktree

Use:
```bash
python3 scripts/syncwheel.py status --fetch
```

Questions that must be answered before edits:
- which remote is the canonical upstream?
- which remote is used to publish branches?
- which branch is the real integration branch?
- is there already a deterministic manifest?
- are there commits on integration that are not mapped to any PR stack?

The primary worktree stays on the manifest integration branch; other branches use dedicated
worktrees. A clean, bounded integration operation may switch it temporarily only if it restores
and verifies the integration branch before completion. Otherwise the mismatch blocks validation
and handoff.

## Phase 2. Recover or create the deterministic manifest

Preferred file:
- `.syncwheel/manifest.json`

If missing, create a starter file:
```bash
python3 scripts/syncwheel.py init
```

Then fill in:
- remotes and canonical base
- integration branch
- stack order inside integration
- one stack id per logical PR
- exact commits for each stack, preferably using `stack sync`, `stack set`, or
  `stack add`
- the state of each stack: `published` by default, or `draft` while it remains
  outside the publication topology

Do not call the workflow deterministic until this file exists and matches reality.

### Active-active handoff for version 2 manifests

When the manifest has `coordination.mode: "active-active"`, inspect its remote
state before changing or publishing managed refs:

```bash
python3 scripts/syncwheel.py handoff
python3 scripts/syncwheel.py reconcile
```

Use the normal full lifecycle to publish a convergent integration projection:

```bash
python3 scripts/syncwheel.py publish
```

Syncwheel atomically publishes changed managed refs together with an append-only
state commit and exact leases. Do not replace it with a raw `git push`. If a
lease reports disjoint stack changes as mergeable, review `handoff` and run
`publish --accept-merge`; overlapping or integration/order conflicts require a
human decision.

### Draft lifecycle

Use a draft when ownership is known but the PR branch is not ready:

```bash
python3 scripts/syncwheel.py stack create exploration --draft
python3 scripts/syncwheel.py stack promote exploration
```

Creation materializes `syncwheel/draft/exploration` immediately and includes the
stack in required integration membership. Rebuilds remain available for drafts.
Promotion selects `pr/<stack-id>` unless `--branch` is given. In active-active
mode it atomically publishes that new ref and a tombstone for the old draft ref;
it never deletes the old remote branch. A retained
`.syncwheel/wt/<old-draft-branch>` directory is reported for deliberate local
cleanup rather than moved automatically.

Use `stack demote <stack>` only after the PR linkage has been removed; Syncwheel
refuses demotion while `github.pr` is populated. Demotion is a state-only
transition and deliberately keeps the existing branch name.

### Sharing a draft between clones

A draft is private to the forge, not to the coordination domain. Under
active-active coordination, `stack push` and `reconcile --push` publish the
draft's source ref to the coordination remote like any other managed ref: same
atomic push, same leases, same coordination state.

```bash
python3 scripts/syncwheel.py stack push exploration
```

That is what makes the draft reproducible elsewhere. A second clone applies the
published coordination snapshot and rebuilds the draft branch from the manifest
alone, without any object from the clone that created it.

Anywhere else the push is refused and names the `draft` state: the stack's
`target_remote`, an explicit `--remote` override, and any manifest without
active coordination. In that last case, keep the draft in the rebuild/validate
cycle until an explicit `stack promote` makes it published.

### Capturing integration-first work

When a non-merge commit was made directly on integration and its eventual PR is
not known yet, create a draft and capture it before the next integration rebuild:

```bash
python3 scripts/syncwheel.py stack create exploration --draft \
  --purpose "Classify integration-first work"
python3 scripts/syncwheel.py stack capture-integration exploration <commit>...
```

Capture first resolves every commit spec, then verifies that the first new
commit was created on the current manifest projection. It adds and deduplicates
the integration SHAs, reuses the normal stack-update projection guard, and only
then rebuilds that one source branch with the normal backup and `stack_rebuilt`
ledger event. The manifest is saved only after the rebuild succeeds. It does
not rebuild or otherwise change integration; run `int rebuild` later when the
projection should be refreshed. Capture uses a temporary worktree and removes it
before returning.

## Phase 3. Validate the manifest against Git

Run:
```bash
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
```

Look for:
- missing commits
- branches missing locally
- commits declared for a PR branch but not contained there
- commits declared for a stack but not present on integration
- integration referring to unknown stacks
- unmapped integration commits: `plan` and `check` offer a new draft plus
  `stack capture-integration` as the durable remedy
- unknown stack states; a missing stack branch remains a warning so reconcile
  can materialize it

## Phase 4. Repair PR branches deterministically

For each stack that needs repair:
1. use the manifest as the exact commit list
2. rebuild the PR branch in a dedicated worktree
3. validate again
4. only then push or update the PR

Dry-run:
```bash
python3 scripts/syncwheel.py stack rebuild <stack> --worktree <path> --dry-run
```

Dry-run output is an executable POSIX shell transcript, not a description of
what Syncwheel might do. Non-plumbing replay lines are the exact shell-quoted
argv (`quoted(argv)`), with replay identity settings only as leading POSIX
environment assignments. Plumbing uses a shell block because the tree and
commit object IDs flow through command substitutions; it remains directly
executable by a POSIX shell.

`--replay-mode plumbing` detects whether Git supports `merge-tree --write-tree`
(Git 2.38 or newer). When unavailable it uses the ephemeral path. A plumbing
conflict never selects another mode: Syncwheel reports the paths and stops with
the literal desk-mode retry command, leaving no checkout to resolve by mistake.
The target branch must not already be checked out; use ephemeral or desk when
it is.

Apply:
```bash
python3 scripts/syncwheel.py stack rebuild <stack> --worktree <path>
python3 scripts/syncwheel.py stack push <stack>
```

For a draft stack, the push step is the coordination-remote publication
described in "Sharing a draft between clones"; it never reaches the stack's
target remote.

If you are already on the target PR branch and the checkout is clean, you can
use in-place mode instead:

```bash
python3 scripts/syncwheel.py stack rebuild <stack> --in-place
python3 scripts/syncwheel.py stack push <stack>
```

## Phase 5. Repair integration deterministically

Integration is not a mystery branch. It is an ordered replay of declared stacks.
By default this replay is a linear `cherry-pick` of declared commits. If the
manifest sets `integration.strategy` to `merge-stacks`, syncwheel instead
merges each declared stack branch in manifest order with `--no-ff`.

Replay carries the source commit's author and committer metadata and disables
clone-local `rerere` and GPG signing for the command. Replaying the same commits
onto the same base therefore preserves their SHAs. For `merge-stacks`, the merge
commit metadata is derived from the tip of the stack being merged, so the merge
history is deterministic as well. The first rebuild after upgrading may rewrite
previous replayed SHAs once; an unchanged rebuild after that is a no-op.

Dry-run:
```bash
python3 scripts/syncwheel.py int rebuild --worktree <path> --dry-run
```

This has the same executable POSIX transcript contract as `stack rebuild
--dry-run`: non-plumbing argv stay shell-quoted exactly, while plumbing renders
the executable object-ID shell block.

Apply:
```bash
python3 scripts/syncwheel.py int rebuild --worktree <path>
python3 scripts/syncwheel.py int push
```

If you are already on the integration branch and the checkout is clean:

```bash
python3 scripts/syncwheel.py int rebuild --in-place
python3 scripts/syncwheel.py int push
```

Rebuilds create a `backup/<branch>-before-syncwheel-<timestamp>` branch first
when the target branch already exists.

### Manifest self-reference rule

If `.syncwheel/manifest.json` is the source of truth for exact stack commit
ownership, do **not** model a commit that edits that manifest as a normal stack
commit inside the same manifest revision.

Why:
- the manifest would need to name the SHA of the commit that changes the
  manifest itself
- updating the manifest to include that SHA creates another manifest-changing
  commit
- that creates an ownership recursion loop

Stable rule:
- treat manifest edits and syncwheel-version bumps as **control-plane metadata**,
  not as stack-owned product commits
- keep that metadata in an admin checkout/branch or a dedicated maintenance PR
  that is intentionally excluded from `integration.stacks`
- rebuild PR branches and integration from the manifest; then validate again

Practical flow:
1. update `.syncwheel/manifest.json` in a clean admin checkout
2. run `python3 scripts/syncwheel.py reconcile`
3. run `python3 scripts/syncwheel.py reconcile --apply --worktree-root <path>`
4. add `--push` when the rebuilt managed branches should
   become the shared remote state
5. rerun `check` or `reconcile`
6. commit/publish the manifest update separately if you want it reviewed, but do
   not expect syncwheel to classify that manifest-maintenance commit as a normal
   stack commit in the same manifest revision

This keeps `main-integration` free of stale cherry-picks and avoids infinite
manifest self-classification.

## Phase 6. Validate honestly

Minimum checks:
- `syncwheel.py validate`
- stack-by-stack branch containment
- integration containment
- typecheck/tests if relevant to the repo
- PR publication state if GitHub is in scope

If a repo has known baseline failures unrelated to syncwheel, record them explicitly.

## Phase 7. Report

A useful syncwheel report says:
- what the manifest now declares
- what PR branches were created or rebuilt
- what integration now contains
- what remains intentionally temporary
- what still needs a human decision

## Minimum success criteria

A syncwheel run is successful only when:
- the branch model is explicit
- the manifest is present and valid
- each real integration change maps to a PR stack
- PR branches can be rebuilt from the manifest
- integration can be rebuilt from the manifest order
- unresolved coupling is named explicitly
