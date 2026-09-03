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

- the normalized public manifest projection, with local remote aliases and
  remote-qualified local refs removed;
- the canonical digest of `.syncwheel/manifest.json` on the exact integration
  tip recorded in `managed_refs`;
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

### Draft source refs

A `draft` stack is withheld from the forge, not from the coordination domain.
Its source ref `refs/heads/syncwheel/draft/<stack>` is an ordinary managed ref:
it is observed, leased, and published in the same atomic push as every other
managed ref, and it appears in the state snapshot's managed-ref map. Publishing
it is what makes the draft reproducible — another clone applies the snapshot and
rebuilds the branch from the manifest alone, holding none of the originating
clone's objects.

The two publication levels stay independent. A draft push is accepted only when
coordination is `active-active` and the resolved remote is the coordination
remote, which `defaults.publication_remote` must equal. Every other destination
— the stack's `target_remote`, an explicit `--remote` override, or a manifest
with coordination disabled — is refused with the `draft` state as the reason.
`publication.enabled: false` continues to gate the PR side only.

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

## Repairing incorrect state evidence

`coordination repair` is a narrow recovery protocol for a managed branch whose
remote tip is already authoritative while the latest state commit records a
different tip. Planning is read-only and emits a digest-bound JSON document.
Apply requires that exact reviewed document, repeats ownership and pending-merge
checks, and stops on any state or managed-ref drift.

The child state is built directly from the validated parent rather than from the
local manifest. It preserves the parent manifest and digest, tombstones, and all
other managed refs, including refs no longer adopted locally. Only publication
identity/evidence fields and the selected managed-ref tip change. The child has
the previous state commit as its sole parent, making both repair and rollback
append-only. Replanning an already repaired ref produces an idempotent no-op.

Ordinary `git push --atomic` cannot provide the required transaction: Git omits
an unchanged managed-ref refspec as up to date, so its lease is never sent. The
apply boundary therefore requires an externally verified write freeze or a
server-side transaction that continuously guards the state ref and every
managed ref. GitHub branch locks do not meet that contract: administrators can
bypass or change them during the operation. The `github-lock` backend therefore
stops as unsupported before any state update.

The `tree-equivalent-state-cas` backend covers one mechanically provable subset
without claiming a lease on an unchanged managed ref. Planning requires the
recorded ref to remain actively declared and owned, fetches the exact observed
commit, and binds the plan digest to both commit tips, their identical tree,
the state tip, the manifest digest, and every managed-ref observation. Apply
repeats those checks and creates an evidence-only state child whose
`changed_refs` is empty and whose `repair_evidence` records the plan digest,
tips, proof type, and shared tree. It then pushes only that child to the state
ref with an exact lease; it never sends an integration or application refspec.

After CAS, Syncwheel independently observes the new state tip and every guarded
ref, then validates the child Git parent, declared state parent, repaired value,
and embedded proof. Any post-CAS drift stops immediately. The accepted child is
not rolled back or rewritten: it remains append-only evidence that the guarded
snapshot was true at CAS time, while the reported failure forces a fresh plan
for later state. Different trees, missing objects, ownership uncertainty,
pre-CAS drift, lease loss, and ambiguous push outcomes all stop fail-closed.
Repository object IDs are currently restricted to 40-hex SHA-1; SHA-256 state
or managed-ref tips are rejected rather than truncated or inferred.

The `fast-forward-state-cas` backend covers a second mechanically provable
subset: an actively owned managed ref whose observed tip is an exact descendant
of the recorded tip. Planning binds the digest to both endpoint tips and trees,
the complete oldest-to-newest commit interval, its count and digest, and every
managed-ref observation. The interval is capped at 1024 commits so the reviewed
proof remains explicit and bounded rather than summarized from an unbounded
history.

Apply recomputes that exact ancestry proof and all guarded observations, then
uses the same state-only CAS boundary. The evidence child records the endpoint
trees and interval digest and count; `changed_refs` remains empty because the
operation never updates the managed branch. A non-descendant tip, an oversized
interval, a changed plan, ownership uncertainty, state lease loss, or any
pre- or post-CAS managed-ref drift stops fail-closed.

### Recorded control-manifest digest forms

A state's `manifest_digest` covers `.syncwheel/manifest.json` as committed on
the integration tip the state itself records. Releases up to 0.42.0 recorded the
digest of the normalized public snapshot derived from that manifest, so a state
published then can never satisfy a file-digest comparison. Verification
therefore recomputes both forms from the same committed manifest and reports
which one matched; a digest that matches neither is still a fail-closed error,
and a digest that only matches a snapshot of some other manifest is not
accepted.

Migration is a publication, not a rewrite. Any publish, stack push, or compose
over a legacy state writes its successor with the control-manifest digest and
records a `coordination_state_digest_migrated` ledger event naming both digests
and both forms. When there is nothing else to publish, the
`state-digest-migration` backend covers the same state-only CAS boundary as the
other evidence backends: the plan reports `digest-migration-required` rather
than `noop`, binds the recorded and expected digests, and refuses any ref
discrepancy. Its child keeps the parent's snapshot, managed refs, and
tombstones byte-for-byte, leaves `changed_refs` empty, and carries the recorded
digest in `repair_evidence`.

## Composing additive stack proposals

`coordination compose` is separate from repair. It handles a known common state
whose local and remote descendants independently add stack records. The plan
binds the exact known base state and its integration-tip control-manifest
digest, current remote state and public snapshot, local proposal, composed
snapshot, source and integration tips, all managed-ref observations, and
unmapped integration commits.

Version 1 is deliberately additive-only: the local side must add exactly the
requested stack, while the remote side may add stacks. Neither side may remove,
edit, rename, or reorder existing stacks; change defaults, channels,
coordination, integration base, branch, or strategy; or alter membership except
by appending exactly its new stack IDs. Same-ID or same-branch additions must be
byte-identical or composition stops.

Apply rederives the digest-bound plan and uses the ordinary coordinated atomic
publication for exactly the new source ref plus the next state child. All
remote-added stacks, prior managed refs, and tombstones carry forward. The
integration ref is an exact guarded observation and receives no refspec, so its
bytes, tip, and unmapped commits are preserved; the child remains `partial`.
After CAS, Syncwheel independently verifies the child, ownership, and every
managed ref before adopting the composed manifest locally. A failed local save
is not retried as publication: the next exact plan is adopt-only when remote
state already proves the intended composition.
