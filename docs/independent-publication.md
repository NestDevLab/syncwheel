# Independent stack publication

## Contract

Publishing one stack must not require an unrelated author's worktree to be
cleaned, captured, unlocked, or removed. A refusal must identify a resource the
requested operation actually needs. Publication, composition, validation, and
cleanup are separate operations with separate preconditions.

This contract qualifies the existing **version 1, delivery-mode, git-tracked**
path. It does not require migration to active-active coordination. The reference
implementation for this qualification is Syncwheel 0.47.31, commit
`b4bce7ff2f0fecb038dcdd32e6cc347cc900c09b`.

## Operation and resource boundaries

| Operation | Resources it needs | Resources it must preserve |
| --- | --- | --- |
| Author a feature | The author's checkout, index, and source branch | Other authors' checkouts and the shared integration projection |
| `stack set --preserve-worktrees` | The selected declaration, manifest transaction, and normal control-metadata persistence | All source checkouts, indexes, worktree registrations, and unrelated branch tips |
| `stack push <id>` | The selected materialized source, its publication ref, applicable ownership/retention proof, and publication receipt | Unrelated source refs and worktrees; unrelated remote product refs |
| Compose and test | Explicit base and selected revisions, a candidate, and exclusive use of the test slot | Authoring worktrees and their uncommitted work |
| Release or cleanup | The explicitly selected resource and its recovery evidence | Unselected worktrees and unpublished work |

A tracked manifest update can create a **local control-metadata commit** under
the repository's persistence policy. Preserving worktrees does not promise that
the integration ref or the entire ledger remains byte-identical. It does promise
that unrelated product work is not captured, staged, reset, or cleaned. Remote
publication authority remains limited to the selected stack.

The manifest and the local registry may need short-lived serialization. A live
transaction lock is temporary contention over shared metadata. It is different
from requiring every author to finish their work before another stack can ship.

## Qualified input and command sequence

The stack's **publication branch must already contain the intended committed
source**. Declaring commits does not materialize a branch. An existing feature
branch with additional committed work is the input to this path; an arbitrary
list of commits from another branch is not enough.

After the repository's normal tracking and ownership checks:

```bash
syncwheel stack set feature-a <source-revision-or-range> \
  --preserve-worktrees "Update this declaration while preserving other authors"
syncwheel stack push feature-a --remote origin
```

Repeat the same scoped sequence for an independent `feature-b`. Inspect each
command's nested `--help` before its first mutation. Use the actual repository's
remote and stack names.

If the source still needs reconstruction, capture, adoption, or draft promotion,
that is an additional operation with its own preflight. Do not treat a successful
`stack set` as proof of a matching source branch, and do not silently add a broad
reconcile or cleanup to the sequence. Retained governed source copies still need
the existing exact-tip, ownership, cleanliness, and retention evidence required
by `stack push`.

Publishing a feature ref is also not proof that a pull request was merged, that
CI passed, or that the combined feature composition was tested.

## Reproducible qualification

`tests/test_independent_publication.py` builds a temporary local Git repository
and bare remote. Its manifest is actually tracked in Git, rather than excluded
under `.syncwheel/`. It uses legacy integration membership, a separate
`main-integration` branch, and two independent materialized publication branches.

The foreign worktree contains staged and unstaged changes, untracked and ignored
bytes, and a symlink. Its snapshot covers the relevant Git refs and HEAD, index,
worktree contents, registry record, and Git retention lock. The test compares
these resources across publication rather than requiring that legitimate events
for the selected stack leave the whole ledger unchanged.

The acceptance cases are:

1. Publish A and then B while foreign C remains dirty and retained. Verify the
   exact remote refs that changed and preserve C after each operation.
2. Interrupt C's cleanup at a real durable cleanup checkpoint, then publish A
   and B without completing C's cleanup as an implicit side effect.
3. Refuse a forced publication that would replace another author's remote
   commit, even after a fetch updates the remote-tracking ref. Preserve that
   remote update and C. Observing a new remote tip does not grant authority to
   overwrite it.

A and B publish sequentially in this fixture. It proves independence from C,
not simultaneous publication by multiple processes. Existing exact-lease tests
separately cover a remote ref changing after the push lease is pinned.

Run the focused fixture from the repository root:

```bash
SYNCWHEEL_UPDATE_MODE=off python3 -m unittest discover \
  -s tests -p 'test_independent_publication.py' -v
```

The tests use local temporary remotes and isolated registry state. They do not
install application dependencies, copy a production checkout, or measure test
slot disk usage. Existing cleanup recovery and publication-lane tests separately
cover idempotent cleanup recovery and refusals for a dirty or changed selected
governed source.

## Command-specific limits

`stack land` has a different contract today: its
`landing_require_clean_worktrees()` precondition visits every registered Git
worktree. It can refuse because C is dirty even when C is unrelated to A. This
global precondition is a known limitation of that command; it is not a
prerequisite for publishing a feature ref with `stack push`.

Successful independent publication does not establish that every Syncwheel
command is independent, nor that a shared integration checkout is safe for
concurrent test processes. Those require separate composition and test-slot
contracts. No new scheduler, ownership database, package-manager migration, or
general weakening of safety guards is required to qualify this publication
path.

## Coordinated selective history validation

Version 0.49.1 also permits `coordination compose --stack <id>` in a version 2
manifest when an unchanged foreign stack has recipe objects unavailable locally.
The existing digest-bound preview/apply protocol still verifies the accepted
remote snapshot and every managed remote ref. Structural manifest validation and
selected history validation remain mandatory. Only exactly unchanged foreign
recipes outside the selected dependency closure may defer history reads. No
author/session contact, transcript or elapsed-time evidence selects this scope.

The preview names `deferredForeignHistory` and sets
`unmappedIntegrationClassificationComplete` to false when any history is deferred;
the apply receipt carries the corresponding snake-case fields. This does not
prove a complete integration classification or qualify a combined test projection.
It preserves the remote integration ref rather than rebuilding it.

This increment does not yet permit missing or drifted managed remote refs, and
does not change version 3 composition or merge-policy integration requirements.
Use the existing exact plan file with `coordination compose --apply --plan-file`.
No global integration rebuild is required by this selective compose operation.

## Scoped ref validation in version 0.50.1

Version 2 `coordination compose --stack <id>` now binds a
`refValidationScope` to the same reviewed plan and publication. The selected
source, its claim, the canonical state and real dependency closure remain
mandatory. Dependencies include explicit declarations and legacy base-owner
relationships resolved on the complete localized manifest.

| Resource | Required evidence | Effect |
| --- | --- | --- |
| Selected source and claim | Exact observed tips and ownership | Atomic publication with the state CAS |
| True dependency refs/history | Exact accepted bindings and available input history | Missing or changed dependencies refuse |
| Unchanged foreign stack | Exact snapshot identity in accepted state | Preserve recipe, recorded tip and claim; no writes |
| Unrelated current foreign ref | Diagnostic observation | Missing/diverged is not alignment proof or a target lease |
| Historical integration anchor | Available object and verified control-manifest digest | Preserve recorded anchor; never bootstrap or publish integration |
| Local control write target | Available exact HEAD, clean primary and manifest/head CAS | Native control-metadata persistence only |

The plan and receipt expose foreign observations as `missing`, `diverged` or
`unchanged`, always unqualified for this operation. Foreign observation changes
alone do not invalidate a reviewed target plan. A changed canonical state or
required resource still invalidates it. No author, session or age decides the scope.

A local product/merge projection may be `unqualified` with a null control suffix.
This is not a passing composition test. The selected source is independently
validated; unrelated local product stays in place. Local bookkeeping may append
a manifest-only control commit, with the previous HEAD as parent and unchanged
product blobs/modes. Unselected private product must not enter any updated remote
ref. No integration realignment or replay is selected by this operation.

After a push interruption, reobserve and replan using the same declared inputs.
An already-published operation adopts its local metadata without repeating the
push. The same scope applies to its verification; unselected refs are never
repaired as an implicit recovery effect. Keep the original receipt until recovery
is verified. Version 3 and non-compose publication retain their existing global
validation contract; this increment does not silently relax those operations.
