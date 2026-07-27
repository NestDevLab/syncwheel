# Active-active coordination

## Purpose

Manifest version 2 lets multiple independent clones publish one Syncwheel
coordination domain safely. It is designed for maintainers working across
devices or automation agents where a shared integration branch must not depend
on one checkout being online or authoritative.

The protocol is deliberately narrow: it coordinates Syncwheel-managed branch
publication. It does not merge arbitrary Git histories, delete remote branches,
or infer stack ownership.

## Manifest versions and migration

Version 1 manifests are legacy and keep their existing push behavior. Syncwheel
never silently upgrades them.

Version 2 requires a `coordination` block. A new `git-tracked` manifest enables
active-active coordination by default when its publication remote is configured:

```bash
syncwheel init --syncwheel-tracking git-tracked --publication-remote origin
```

If no usable publication remote is configured, that initialization fails with
an actionable error. An explicit opt-out persists `mode: "disabled"`:

```bash
syncwheel init --syncwheel-tracking git-tracked --no-coordination
```

Legacy, `local-only`, and previously disabled manifests opt in explicitly:

```bash
syncwheel coordination init --remote origin --apply
```

The command changes only the local manifest. For a `git-tracked` repository,
commit the resulting manifest change through its normal review process. The
remote coordination state is created only by the first successful coordinated
publication.

To opt out again without deleting recovery history:

```bash
syncwheel coordination disable --apply
```

## Manifest contract

```json
{
  "version": 2,
  "syncwheel_tracking": "git-tracked",
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "origin",
    "base_branch": "main",
    "base_ref": "origin/main"
  },
  "coordination": {
    "mode": "active-active",
    "id": "default",
    "remote": "origin",
    "state_branch": "syncwheel/state/default",
    "gc": {
      "worktree_grace_days": 7,
      "backup_retention_days": 30,
      "backup_keep": 2
    }
  }
}
```

`coordination.remote` must equal `defaults.publication_remote`. The state
branch is fixed as `syncwheel/state/<coordination-id>`, so coordination domains
are visible and unambiguous. A remote ref can be owned by only one active
coordination domain.

`disabled` is a persisted opt-out. It retains the version 2 manifest shape but
does not create or update a remote coordination state branch.

## Publication protocol

The state branch is an append-only chain of Git commits. Each state snapshot
contains:

- the normalized public manifest projection and its digest, with local remote
  aliases and remote-qualified local refs removed;
- the complete observed managed-ref map and the refs changed by this publication;
- publication scope and projection status;
- Syncwheel protocol version, state parent, publication UUID, and tombstones;
- a pseudonymous per-installation UUID.

State snapshots deliberately exclude local worktree roots, stack metadata, local
remote aliases, host details, usernames, filesystem paths, credentials, and local
policy. Syncwheel records remote base refs as typed JSON values with a portable
`canonical` or `publication` role, never by a local alias. Explicit Git refs
remain strings and round-trip unchanged. A base ref using another remote is
ambiguous and fails closed rather than being treated as equivalent. State
commits use the fixed `Syncwheel Coordination <coordination@syncwheel.invalid>`
Git identity rather than a maintainer identity.

Before any active-active publication, Syncwheel probes the publication remote
for atomic push support. It then pushes all selected managed refs and the new
state ref in exactly one `git push --atomic`, each protected by an exact lease.
There is no non-atomic fallback: unsupported remotes fail closed.

Before creating that push, Syncwheel also compares the local manifest with the
latest published state. It refuses a stale local manifest that would drop a
remotely published stack, change branch ownership, or overwrite a stack whose
published ref is not a safe successor. Run `handoff` and reconcile the manifest
instead of letting an older clone erase another device's work.

These surfaces use the same publisher when coordination is active:

- `syncwheel publish` and `syncwheel reconcile --apply --push`;
- `syncwheel stack push <stack>`;
- `syncwheel int push`.

`stack push` and `int push` deliberately publish `partial` state snapshots.
Only a full `publish` can claim a `convergent` projection, after every managed
local ref matches the manifest projection.

## Concurrent publications

Run this before handing a repository to another device or agent:

```bash
syncwheel handoff
```

On a failed state lease, Syncwheel fetches and classifies the newer state:

- an equivalent state is accepted as already published and tree-equivalent local
  managed refs are aligned to its published tips;
- disjoint stack-ID changes, with unchanged shared integration configuration and
  ordering, are reported as mergeable;
- overlapping stack changes, integration/order changes, ownership conflicts, and
  other conflicts stop publication.

There is no silent retry and no arbitrary Git history merge. After reviewing a
reported disjoint change, explicitly rerun the full lifecycle with:

```bash
syncwheel publish --accept-merge
```

The command verifies that the remote state did not move, applies only the
approved manifest-level merge, and performs a fresh coordinated publication.

## Closing stacks and local cleanup

Closing a stack publishes a tombstone but never deletes its remote branch:

```bash
syncwheel stack close feature-a
```

`handoff` and `gc` report cleanup candidates. After a successful `sync` or
`publish`, Syncwheel automatically reaps only local artifacts that are all of:

- tombstoned for at least seven days;
- clean, non-current, unlocked, and not covered by an active local lease;
- inside the declared Syncwheel worktree root; and
- recoverable from the published remote branch and state.

Reusing a closed branch ref makes its old tombstone ineligible immediately. The
next successful coordinated publication supersedes that tombstone, so GC cannot
reap a branch that has become active again.

The matching local worktree and stale local branch can then be removed. Backup
branches keep the two newest backups and retain older backups for 30 days.
Remote deletion is intentionally out of scope and always requires a future,
explicit operation.

Use local locks to retain a worktree during investigation:

```bash
syncwheel worktree lock feature-a
syncwheel worktree unlock feature-a
syncwheel gc
syncwheel gc --apply
```

A lock can be released by stack ID even after that stack has been closed.
