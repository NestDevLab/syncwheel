# Agentwheel revision provider

`syncwheel revision-provider` is the Git revisioning adapter for an Agentwheel
mutation policy. It accepts exactly one JSON request on standard input, emits
exactly one JSON response on standard output, writes diagnostics to standard
error, and never publishes a remote ref.

The provider is opt-in. A workspace selects it through Agentwheel configuration;
Syncwheel does not intercept ordinary Agentwheel or Git commands.

## Operation sequence

1. Agentwheel persists its durable start receipt before invoking the provider.
2. Agentwheel sends `check` while the integration checkout, index, and worktree
   are completely clean. This validates the repository mode, tracking policy,
   integration HEAD, manifest, hooks, and absence of unmapped commits. An
   active-active repository also performs a fresh read-only handoff check.
3. Agentwheel performs the mutation.
4. Agentwheel sends `preflight` with the exact changed paths. Syncwheel verifies
   each `beforeSha256` against `expectedHead`, each `afterSha256` against the
   worktree, a clean index, and the absence of undeclared dirty paths. It then
   repeats the fresh active-active handoff check and persists the operation
   lease only if the observed state is still aligned. The lease includes the
   raw index hash and the one-time resolution of `defaults.base_ref`. Protocol
   v1 accepts only an exact lowercase 40-hex commit SHA or an unambiguous direct
   ref. Accepted direct forms are branch/tag shorthand, `remote/branch`,
   `heads/...`, `remotes/...`, `tags/...`, and their full `refs/...` names.
   Syncwheel expands only those explicit candidates and never asks Git to DWIM a
   revision expression. Reflog, upstream, and push selectors, other revision
   expressions, abbreviated SHAs, and symbolic refs (including remote `HEAD`
   aliases) are rejected. A direct ref records and leases both its ref object
   and peeled commit; a ref that resolves to the integration, stack, or channel
   branch set is rejected before a journal or managed ref can be created. The
   operation-owned stack stores the peeled 40-hex commit as its immutable
   manifest base, never the input shorthand. The provider projects the candidate
   on that base and compares every declared product blob, normalizing a missing
   path to the same absent value on both sides and deliberately ignoring mode.
   A `projected` result with matching blobs takes the ordinary `manifest-base`
   draft-stack route. Every other result, including `empty`, can take the
   `derived` route only in a v3 manifest and only when every path is contained by
   `integration.derived_paths`; it creates a provenance-bound commit on
   integration and no draft stack, branch, or manifest mutation.
   The provider persists `projectionRoute` and the selected object ids before
   any ref moves. Recovery follows that route, treats the candidate as
   immutable, and recomputes the route proof; any route or object mismatch
   fails closed instead of replacing an already hook-validated commit.
5. `finalize` captures each changed file through descriptor-bound, no-follow
   reads, writes those exact bytes as Git blobs, and constructs both the product
   commit and its draft projection in the object database. A non-reproducing
   projection outside the derived-path policy stops before any managed ref
   moves; a conflict reports its exact NUL-delimited Git paths and projection
   base. Product hooks then run before
   the provider acquires the absent draft ref with compare-and-swap; only proven
   draft ownership permits the integration ref to advance. A separate
   manifest-only control commit completes local ownership. `stack land`
   compares the declared product projection independently from that control
   commit, so the resulting `manifest-base` stack remains landable.
6. Agentwheel may send `recover` repeatedly after an uncertain result. Recovery
   resumes only a previously journaled operation with the same intent digest. If
   the integration composition or its leased `derived_paths` changes while a
   derived receipt is pending, the provider writes its terminal journal first,
   then appends one
   `revision_provider_expired` ledger event with its recorded decision and the
   named remedy: run a
   new Agentwheel update. Later `finalize`, `recover`, or matching `preflight`
   requests reject with that same terminal reason; the provider never leaves a
   permanently pending receipt. `check` consults the journal first and returns
   the same terminal error for that operation id.
7. `release` may remove only a `prepared` lease for which no Git ref or manifest
   mutation occurred. It does not discard the Agentwheel file changes.

A repeated matching `preflight` always returns the canonical wire status
`prepared`, even when the journal has progressed internally. The caller then
uses `recover`; internal phase names are deliberately not part of the preflight
wire contract.

The operation id becomes the draft stack id `agentwheel-<operation-id>` and the
branch `syncwheel/draft/agentwheel-<operation-id>`. Neither ref is pushed by this
protocol.

Active-active mode fails closed when its coordination state is uninitialized or
unreachable, its manifest or remote managed refs differ, a current local managed
ref differs, another coordination domain claims a ref, or local pending-merge,
lock, or publication-lease state exists. Offline revision preparation is not
supported in active-active mode. A successful preflight records the fresh state
tip in the local operation journal; later publication remains a separate,
explicit coordinated operation. Manifest-v3 coordination snapshots include
`integration.derived_paths` and bounded `integration.derived_provenance`
records. Snapshot application and additive composition preserve both, so a
fresh peer can classify a published derived tip and retain a stale blocker even
after its own rebuild drops that commit.

## Protocol version 1

Every request contains these fields; only `expectedManifestDigest` is optional:

```json
{
  "protocolVersion": 1,
  "action": "preflight",
  "operationId": "01-agentwheel-install",
  "repositoryRoot": "/absolute/canonical/repository",
  "expectedHead": "0123456789abcdef0123456789abcdef01234567",
  "expectedManifestDigest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "commandName": "agentwheel install",
  "reason": "Install the reviewed workspace configuration.",
  "noCommit": false,
  "paths": [
    {
      "path": "config/workspace.json",
      "beforeSha256": null,
      "afterSha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

An absent file uses `null`. A path must be normalized, repository-relative POSIX
syntax and must name a regular file or an absent file. Before and after hashes
must differ. Git internals, Syncwheel control state, symlinks, ignored new files,
duplicate paths, unknown fields, unknown actions, and unknown protocol versions
are rejected. Protocol v1 deliberately rejects mode-only changes: identical
before and after hashes cannot carry an executable-bit lease. When bytes also
change, the captured `100644` or `100755` mode is bound to the candidate tree.
Line feeds and leading or trailing spaces remain significant path bytes; every
Git path list is requested with `-z`, split only on NUL, and never stripped.

Every response contains:

```json
{
  "protocolVersion": 1,
  "providerId": "syncwheel",
  "action": "check",
  "operationId": "01-agentwheel-install",
  "ok": true,
  "status": "ready"
}
```

`finalize` and `recover` additionally return `expectedHead`, `resultingHead`,
`productCommitSha`, `draftStackId`, `draftBranch`, `draftTipSha`, `controlCommitSha`,
`manifestDigest`, `unmappedIntegrationCommits`, and `published`. Nullable fields
remain present on a rejected response so the caller can reconcile an uncertain
outcome without parsing stderr. Draft stack id, branch, projected tip, and
control commit are an all-or-none ownership tuple on the wire: partial internal
progress remains recoverable from the local journal but is not reported as
terminal ownership. Every rejected response is normalized to this strict wire
schema: invalid or abbreviated SHA values become `null`, duplicate or malformed
unmapped SHAs are removed, and both the JSON error and stderr diagnostic are
bounded to at most 4096 JavaScript/UTF-16 code units.

## Crash recovery and ownership

The provider stores one mode-`0600` journal per operation under
`<git-common-dir>/syncwheel/revision-provider/`. Atomic writes and a repository
wide lock protect these durable phases:

```text
prepared -> product_committed -> stack_owned -> control_committed -> verified
         \\-> expired (manifest invalidated; run a new Agentwheel update)
```

Candidate blob, tree, projection, and commit object ids are journaled before
their compare-and-swap ref updates. Every fresh preflight snapshots all managed
local and remote-tracking refs as typed observations: full name, direct or
symbolic kind, resolved object OID, and immediate symbolic target. Hook and final
snapshots compare all four fields. Each ref transaction leases every unaffected
snapshot plus its exact target predecessor. A direct manifest base ref is leased
at its exact ref-object SHA through every ref transaction and final verification.
The persisted stack base is always its peeled manifest-base commit SHA. Derived
receipts instead lease the ordered integration composition (base, strategy, and
declared stack commits) plus the exact ordered `derived_paths`, so unrelated
manifest edits do not expire them while a path-policy change does.
Projection never re-resolves a moving base. Recovery accepts only the expected
parent or that exact candidate. It does not reset, rebase, force-update, delete a
branch, or infer ownership from a similar commit.

Fresh preflight also journals the complete expanded ref-transaction set,
including every immediate symbolic referent. Before every recovery attempt,
already-installed ref shortcut, final verification, repeated preflight, release,
or already-verified response, the provider derives the corresponding lock paths
only from those typed journal observations and requires all of them to be absent.
An unknown surviving lock is preserved and named; the provider never guesses its
owner or deletes it automatically.

Git 2.43 has no transaction command that verifies a symbolic target string.
Syncwheel therefore sends `option no-deref` before every `update-ref --stdin`
operation, prepares the transaction to lock each exact ref name (including every
symbolic referent), then rechecks the typed observations while those locks are
held before committing. This avoids duplicate locks for `origin/HEAD` and its
referent and makes type or same-OID retargeting fail closed for cooperative Git
writers. Git lockfiles are not a security boundary: a process that overwrites ref
storage directly can bypass them, although hook and final typed snapshots still
detect the resulting local state change. An orderly provider error aborts the
prepared transaction and releases those locks. If the provider and its Git
process group are killed after `prepare`, Git 2.43 may leave loose-ref or
`packed-refs.lock` files, plus `HEAD.lock` when the checked-out branch was part
of the transaction, whose ownership is not journal-provable. Recovery never
deletes them automatically: it fails closed, lists only the exact detected lock
paths, and requires an operator to prove no Git writer is active and remove
those specific files before retrying `recover`.

Git's executable `reference-transaction` hook runs inside these managed
`update-ref` transactions and is trusted code. On Git 2.43, `prepared` runs
before any target rename and `committed` was observed only after the transaction
locks were cleaned up, so neither notification is a valid pause or ownership
receipt for the narrower interval between one target rename and cleanup of the
remaining locks. If the provider process group is killed in that interval, the
journal-derived recovery gate rejects even an otherwise idempotent candidate
shortcut or terminal receipt until an operator proves that no writer is active
and removes exactly the listed locks. The hook can also perform arbitrary local
or external side effects that the provider cannot prove or undo; repositories
must keep it deterministic and side-effect-free.

The real Git index is also an exact lease. For product and control alignment,
the provider prepares and refreshes the complete replacement index separately,
durably journals an operation-specific backing file, and fsyncs it before
acquiring Git's `index.lock` as a hard link to that file. The shared inode is a
provable ownership token: after `SIGKILL`, recovery may remove and reacquire only
that exact journaled lock, while an unrelated lock remains untouched and causes
a fail-closed rejection. With the lock held, the provider rechecks the
predecessor byte hash, atomically renames the replacement, and fsyncs the parent
directory. A concurrent staged or index write is retained and the operation
stops; it is never overwritten. The successfully installed hash is journaled as
the predecessor for the next phase, including recovery after the narrow windows
before rename and between rename and journal persistence.

The draft projection never uses a worktree or `cherry-pick`. Syncwheel applies
the product delta with `merge-tree` and routes only blob-reproducing results to
the deterministic draft ref. A non-reproducing allowed lock-only result is a
derived integration commit carrying two real Git trailers parsed by
`git interpret-trailers --parse`:

```text
Syncwheel-Derived-Projection: <operation-id>
Syncwheel-Derived-Paths: <sha256>
```

The digest hashes each declared path in sorted order as
`path NUL resulting-blob-id NUL`; a deletion uses an empty blob id. A commit is
derived only when it is non-merge, every exact changed path is under
`integration.derived_paths`, both trailers match the recalculated content, and
the same operation, commit, paths, and path digest have a durable provenance
record, which also retains the integration composition digest. Trailer-like
body text, a syntactically valid unknown operation id, or path-only
classification does not qualify.

With active-active coordination, the published snapshot's
`integration.derived_provenance` list is the shared source. The author ledger
supplies unpublished local updates and is reduced over that shared base. A
repository without coordination has one clone and uses only its local ledger;
there is deliberately no Git-ignored `.syncwheel/derived-provenance.json`.
Rebuild still drops derived commits. `validate` and maintenance planning through
`plan` compare retained provenance with integration and report
`derived-projection-stale`, affected paths, and the remedy to run a new
Agentwheel update on every peer. A new update replaces provenance only for the
same complete declared path set: a new derived route replaces the record, while
a manifest-base route resolves it. Conflict diagnostics use Git's
NUL-delimited name-only output and name both paths and base.

The accepted cost of the derived route is that its lock never reaches
`origin/main` through Syncwheel. It can reach `main` only through a later update
whose composition equals the manifest base, making the verified route
`manifest-base`. This is intentional: a lock derived from unlanded composition
cannot truthfully be landed on `main` by itself.

The product commit contains only the declared product paths. Its full reason and
`Agentwheel-Operation` trailer are preserved when the draft is replayed. The
second commit contains only `.syncwheel/manifest.json`; ledger and provider
journals remain local operational state. Final verification requires a clean
repository, the original set of worktree paths and remote-tracking refs, and zero
unmapped integration commits. Manifest replacement and its ledger event are
separate recoverable journal steps: recovery accepts only the exact desired
manifest digest and appends at most one operation-specific ledger event.

Ledger JSONL writes use newline framing, an exclusive ledger lock, fsynced event
records, atomic fsynced checkpoints, and directory fsync. Recovery may complete
the missing newline of a valid final JSON object or truncate an invalid
unterminated suffix; it never rewrites a newline-terminated record. A durable
event remains authoritative when its derived checkpoint is absent or stale. A
manifest-invalidated pending receipt writes its journal terminal record before
the matching `revision_provider_expired` event; it is
not safe to retry that receipt after the manifest lease is gone.

Before the first product ref update and before the control ref update,
executable `pre-commit` and `commit-msg` hooks run against the exact temporary
index and deterministic message for that candidate. Hook rejection moves no
subsequent provider ref and recovery retries the same journaled candidate. A
hook that changes any Git ref (including tags), symbolic `HEAD`, the complete
`git worktree list --porcelain` state, the real or temporary index, declared
worktree bytes, or the commit message is rejected before the corresponding ref
mutation. Executable `prepare-commit-msg` hooks are unsupported and fail closed,
because accepting their message mutation would make the prepared commit
nondeterministic. Executable hooks are trusted code: the provider can detect
their observable local repository side effects, but cannot prove or undo an
external effect such as a network request or `git push`. Repositories must not
enable such side effects in these validation hooks.

`noCommit: true` records the verified opt-out without creating either commit or
the draft stack. An empty `paths` array converges as `no-repository-delta` and
also creates no stack.

## Terminal handoff: owned but unpublished

A successful `status: "verified"` result means **owned-but-unpublished**, not
delivered. On `manifest-base`, the local draft ref, product commit, manifest
ownership, and control commit are complete. On `derived`, only the
provenance-bound integration commit is owned and all draft/control fields are
null. In both cases `published` remains `false`, and neither a remote branch nor
coordination state was updated.

`draftTipSha` is the exact projected tip of `draftBranch`. It is intentionally
distinct from `controlCommitSha`, which is the manifest-only commit at the
integration HEAD.

Use this explicit handoff after reviewing the receipt and journal:

```bash
syncwheel validate
syncwheel handoff --json
syncwheel stack push agentwheel-<operation-id> --dry-run
syncwheel int push --dry-run
```

Run either non-dry-run push only under its separate delivery approval. In an
active-active repository, if `handoff` reports that the remote state advanced
from the journal's recorded `coordination.stateTip`, plan the additive merge
from that exact base instead of attempting a stale push:

```bash
syncwheel coordination compose \
  --stack agentwheel-<operation-id> \
  --known-base-state <journal-coordination-state-tip> \
  --known-base-snapshot-digest <journal-coordination-manifest-digest> \
  > compose-plan.json
syncwheel coordination compose --apply --plan-file compose-plan.json
```

The second command publishes and therefore requires separate review and
approval. The provider never invokes any of these handoff commands itself.
