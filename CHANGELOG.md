# Changelog

## 0.43.5 - 2026-09-05

- Include `.gitignore` in the same path-limited transaction commit as the
  tracked manifest when Syncwheel itself upgrades the managed ignore block.
  Existing repositories that first learn a new managed metadata pattern no
  longer remain dirty after an otherwise successful stack mutation, while
  pre-existing `.gitignore` changes are still rejected by the normal clean
  checkout guard rather than silently captured.
- Exercise the complete pre-upgrade flow: `stack create` upgrades the managed
  ignore block, commits exactly `.gitignore` and `.syncwheel/manifest.json`,
  and leaves `git status --porcelain` empty.

## 0.43.4 - 2026-09-05

- Commit a `git-tracked` Syncwheel manifest at the successful end of the same
  locked transaction that writes it. The commit is restricted to the manifest
  path, leaves unrelated staged and unstaged work untouched, and is skipped for
  `local-only`, personal, and external manifests. Stack mutations therefore no
  longer leave their tracked coordination contract dirty, while the existing
  manifest lock and active-active leases continue to serialize concurrent
  writers. No command publishes the commit automatically.
- Allow reconciliation and integration rebuild/alignment preflights to ignore
  every status under `.syncwheel/`, including tracked modifications, instead of
  only untracked entries.
- Ignore personal manifest ledger directories matching
  `.syncwheel/manifests/*.local-ledger/` in the managed Git ignore block.

## 0.43.3 - 2026-09-04

- Narrow the managed ref guard to the surface it protects. `hooks ref-guard`
  now classifies the incoming transaction first and leaves before any Git call
  when it carries no branch ref, so remote-tracking updates from `git fetch`,
  the pseudo-refs `git worktree add` writes, tags, notes and stash are no
  longer refused. When `guard.json` is missing or malformed the guard still
  refuses moves of the integration ref and manual commits on it in the primary
  checkout: with no usable configuration it cannot tell which branch is the
  integration one, so it refuses every branch ref and names
  `syncwheel hooks install --apply` as the repair. Fetch, worktree creation,
  tags, notes, stash and `HEAD` keep flowing, so a repository whose hooks
  predate `guard.json` stays usable instead of rejecting every ref
  transaction.
- Stop Syncwheel's child Git processes from inheriting `GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_PREFIX` and
  `GIT_NAMESPACE` from the hook environment. A guard running inside a linked
  worktree pointed its `git -C <other worktree> status` at the calling
  worktree's index and rewrote it, after which the merge that triggered the
  hook died with `fatal: stash failed`. An explicit `env=` override still wins.
  This applies to every Syncwheel invocation, not only to hooks, so
  `GIT_DIR=... syncwheel <command>` from outside a checkout no longer selects
  the repository; pass `-r <path>` instead.
- Skip the workspace-wide preflights for the `hooks guard`,
  `hooks worktree-guard` and `hooks ref-guard` callbacks. They run inside a Git
  process that is mid-operation and must decide about the incoming refs, not
  scan other worktrees.

## 0.43.2 - 2026-09-04

- Fix `validate_coordination_publication_base` (A14) to run the integration-ref
  successor check whenever `integration_ref` is in the operation's
  `changed_refs`, as the amendment specifies, instead of only for `int push`
  and `reconcile --apply --push`. The narrower scope condition let an ordinary
  `stack push` from a stale-but-honest clone silently overwrite another
  clone's already-published integration tip with `rc 0`.
- Add a direct unit test isolating the claim proof's scope check (L2) from its
  changed_refs check (L3), covering the gap noted in the round-11 review where
  the scope check could be removed without failing the existing test.

## 0.43.1 - 2026-09-03

- Fix the claim-proof changed_refs test fixture to match a real ref-set
  mismatch under A9's control-manifest fold-in instead of a stale ref count.

## 0.43.0 - 2026-09-03

- Publish each active-active draft's source ref and coordinated state atomically
  when it is created, so independent drafts cannot strand one another before a
  normal stack publication or close.
- Serialize ownership of every managed source ref through an atomically advanced
  `syncwheel/claim/heads/...` ref. Existing manifests default to advisory claim
  validation; `coordination claims backfill` prepares an explicit move to
  required validation without overwriting a foreign claim.
- Close never-published drafts remote-first: record an intent, atomically publish
  a tombstone claim and state child, save the manifest, then terminalize the
  ledger event. A retry completes only from the same tombstone generation; a
  later claim produces the terminal `close_superseded` outcome instead.
- Create draft refs with create-only compare-and-swap, reject non-CAS worktree
  reattachment, and recover interrupted create operations from generation-bound
  ledger intents. The create claim carries the intent token, and a retry removes
  its deterministic temporary worktree registration after process death.
- Journal every coordinated publication intent before its atomic push. A retry
  whose exact token is already present in claim and state completes locally
  without advancing either ref again.
- Prove that a coordinated operation landed from its own operation token in the
  published state chain instead of from current ref tips, so a legitimate push by
  another clone on the same ref no longer reports a landed operation as
  superseded, undoes its local rename, or writes that claim to the ledger.
- Terminalize coordinated publication intents. An intent whose token is in the
  published state chain completes as `already_published`; one that never reached
  the remote is abandoned as `coordination_publish_abandoned` by the next
  publication, `reconcile`, or `resume`, with reason `not_landed` when nothing
  else published meanwhile and `superseded` when the reviewed state tip was
  overtaken. A lost race, a push the remote refuses, or process death before the
  push no longer blocks further coordinated publication from that clone.
- Complete a promotion whose push landed before its manifest was saved from the
  published state: `stack promote`, `stack push`, `int push`, `stack create`,
  `stack close`, and `reconcile --apply` all finish it, rebuilding the promoted
  branch and dropping a rematerialized draft instead of requiring an exact local
  branch layout.
- Name the pending intent's own remedy command when one blocks a publication.
- Decide `close_superseded` from the claim of the pending close generation
  instead of the shared state tip, so an unrelated publication no longer aborts
  a retry that only had to complete an already-published tombstone.
- Report an unreachable coordination remote during `stack push`, `int push`,
  `stack promote`, `publish`, and `reconcile --apply --push` as an operational
  failure that names the retry command, and report an atomic push the remote
  refused without moving anything as a rejection with the same retry command.
- Adopt a pending reconcile publication intent only when it matches the
  fingerprint of the current operation.
- Prove `absorbed` against an explicitly fetched delivery SHA by comparing the
  composed stack result for a non-empty NUL-separated touched-path set,
  including squash-equivalent deliveries while rejecting odd-path omissions
  and content removed by a later revert.
- Serialize every coordinated publication cycle in a clone behind
  `<git-common-dir>/syncwheel/coordination-publication.lock`, held from the
  intent through the push, the local remainder and the terminal event. A second
  command exits 2 naming the live owner's pid instead of terminalizing an intent
  that is still in flight, and a plain retry after it finishes succeeds. Process
  death releases the lock, so the next command still recovers a dead owner's
  intent; dry runs and `handoff` take no lock.
- Record the owner (installation, host, pid, process start time, lock token) in
  every publication intent and name it in the abandonment event. Intents written
  by earlier versions stay terminalizable and are marked `legacy_intent`.
- Refuse a second terminal ledger record for one operation token.
- Prove that a coordinated operation landed from a state commit inside the
  intent's own window that declares its token, scope, changed refs, projection
  status and manifest digest, and from a claim carrying that token for every
  touched source ref. A token reused for a different operation, a state chain
  scanned before the recorded expected tip, and an intent with no recorded
  expected tip no longer prove a landing, and a claim still carrying the token
  proves one the state chain no longer carries.
- Undo the local rename of an abandoned promotion only when the remote carries
  neither the promoted ref nor a claim bearing that operation token.
- Derive the coordination token of a channel publish or close from the operation
  itself instead of the caller's `--operation-id`, which stays the clone-local
  idempotency key, and record it in the channel operation events so a retry
  recovers the same token.
- Record and terminalize a publication intent for
  `coordination claims backfill --apply`, whose claims now carry that operation
  token, and name the retry command when the coordination remote is unreachable
  during it or during `stack create --draft`.
- Complete a landed promotion at the head of `stack rebuild`, `stack sync` and
  `stack add` as well, and anchor a diverged rematerialized draft branch under
  `refs/syncwheel/recovery/drafts/` before dropping it, so a promotion that
  landed before its manifest save can no longer block unrelated stacks or name a
  remedy that refuses itself.
- Record the publication scope and the operation's whole ref set in every
  coordination claim, and require both from the claim evidence a recovery
  accepts. A claim that carries the operation token under another scope or
  another ref set proves a different operation, so the pending intent is
  abandoned instead of completed. Claim evidence stays confined to the recovery
  paths: a publication still decides from the state chain alone.
- Fingerprint a coordinated operation over the public manifest snapshot without
  `integration.derived_provenance`, which the intent and the published state
  resolve from different sources by design. On a manifest v3 clone whose
  remote-tracking state is behind, a retry of the interrupted command now
  completes from the published state instead of publishing it a second time.
- Take no exclusive publication lock on the planning and dry-run paths, so
  `reconcile`, `resume`, `stack push --dry-run`, `int push --dry-run` and
  `stack rebuild --dry-run` report the state instead of being refused for the
  duration of another command's network push.
- Refuse a second terminal ledger record when it is written rather than when the
  ledger is read, and name `ledger show` as the command that inspects one, so a
  ledger that already carries two stays readable.
- Prune the publication lock's retained stale inodes with the governed worktree
  registry's, instead of leaving one behind for every recovered dead owner.
- Name the retry command when a coordinated publish stops because the remote
  state changed after the reviewed plan.
- Stop the coordinated-publish control-manifest bootstrap from saving the
  caller's in-progress manifest to disk before the caller's own network push,
  so a remote-first close still reaches the remote before anything local is
  written even on a clone whose integration branch has never been published.
- Compute a published coordination state's `manifest_digest` from the control
  manifest committed at its own `managed_refs[integration_ref]`, carrying the
  value over from the parent state when the operation does not touch the
  integration ref, so the digest a clone writes always matches what
  `classify_coordination_state_manifest_digest` recomputes on read.

## 0.42.7 - 2026-09-03

- Add the `state-digest-heal` coordination repair class for a manifest_digest
  that matches neither the raw control-manifest digest nor the legacy
  snapshot form. This happens when a prior repair moved a managed ref while
  the manifest it pointed at genuinely changed: repair preserves the
  parent's digest byte for byte by design (it corrects transport evidence,
  not topology), leaving the digest orphaned even though the ref itself is
  correct. Previously `coordination_repair_plan` classified this state as
  neither raw nor legacy, swallowed the error, and reported `noop`, leaving
  every publish path permanently blocked. The plan now reports
  `digest-heal-required` and recomputes the digest from the manifest already
  committed at the aligned integration tip; apply appends a state child that
  changes only the recorded digest, under the same state-only CAS boundary as
  the other evidence backends, and records
  `coordination_state_digest_healed` in the ledger with both digests. A ref
  that also needs its own topology repaired is never healed alone (topology
  repair keeps priority), and an unreadable or missing registered tip stays a
  fatal error rather than a healing candidate.
## 0.42.6 - 2026-09-03

- Fix the GitHub adapter to read commit identities from the `authors[]` shape
  returned by `gh pr view`, while preserving fail-closed records for unresolved
  commit authors and supporting the legacy author shape.
- Add regression coverage for resolved and unresolved commit author identities.

## 0.42.5 - 2026-09-03

- Add a private, clone-local GitHub PR merge policy with strict provenance
  filters, dry-run/apply/clear CLI commands, and preservation of unrelated
  profile keys.
- Add the fixed `syncwheel-github` adapter and digest-bound
  `stack merge-pr` plans with exact head pinning, review-only admin bypass,
  CI/rules/thread gates, post-merge verification, idempotent reconciliation,
  and ledger receipts.
- Document the fail-closed merge contract and add focused policy, adapter,
  preflight, and transaction tests.
- Guard the shared primary checkout against manual integration commits and tracked
  changes before built-in mutations, with manifest-derived capture or queue remedies,
  a visible reasoned opt-out, persistent common-Git guard state, and fail-closed
  stable installed hooks with single-use authorization nonces. Required hook status
  is explicit: absent hooks do not block ordinary Syncwheel commands, while an
  installed hook without its stable CLI fails closed and reports as degraded.
- Make `guard.json` the atomic common-Git source of truth, run Syncwheel before every
  chained hook while propagating either failure, preserve live concurrent-process
  nonces, reject CLI paths from repository/lane/worktree state, audit disable intent
  before removal, and derive mutation preflight from one exhaustive command table.
- Honor shared, personal, and explicit manifest selection throughout guard lifecycle;
  validate every guard-state read and expose branch drift as degraded; bind nonces to
  process-start identity, audit stale malformed cleanup, and extend mutation coverage
  through revision-provider method savers and the exact recovery-remedy set.
- Treat non-UTF-8 guard state as degraded and explicitly repairable; require and ledger
  guard retarget reasons; keep partial bundles in degraded mode; rebaseline changed
  chained user hooks without overwriting them; and consolidate command and internal
  writer policy in one entrypoint registry with execute-time manifest classification.
- Document that git 2.54 runs the `reference-transaction` hook in a pre-lock
  `preparing` phase, where a chained user hook rejects before the guard decides
  in `prepared`.
- Stop counting repo-local Syncwheel state as primary-checkout dirt, so a
  git-tracked manifest a Syncwheel command just wrote no longer refuses the next
  mutation with capture and lane remedies that cannot move it.
- Let the reasoned guard opt-out lift the dirty-primary refusal too, so a clone
  that deliberately keeps shared work in its primary checkout can still run
  built-in mutations. An enabled guard still refuses, and the warning stays.
- Register the guard install and remove lifecycle and `journal schedule` as
  internal ledger writers, so every writer is classified in the single
  entrypoint registry without pulling recovery commands into the global
  manifest transaction.

## 0.42.3 - 2026-09-03

- Accept both recorded control-manifest digest forms in coordination state:
  the digest of `.syncwheel/manifest.json` on the state's integration tip, and
  the normalized public snapshot of that same manifest recorded before 0.42.2.
  Verification reports which form matched; any other value still fails closed.
  Without this, every publish, stack push, and compose from a state published
  before 0.42.2 stopped with no available publication path.
- Migrate a legacy state on its first successful publication: the successor
  carries the control-manifest digest and the ledger records
  `coordination_state_digest_migrated` with both digests and both forms.
- Add the `state-digest-migration` repair backend for a legacy state whose
  managed refs are already coherent. The plan reports
  `digest-migration-required` instead of `noop` and binds both digests; apply
  appends a state child that changes only the recorded digest under the same
  state-only CAS boundary as the other evidence backends.

## 0.42.2 - 2026-09-03

- Preserve the control manifest across integration rebuilds: reject an
  unexplained pre-rebuild manifest divergence, then restore and commit the
  manifest when the rebuild itself replaces it. The manifest-only control
  commit is built deterministically with an isolated index, verified before
  its integration ref CAS, and recorded with actor, command, and reason.
- Finish control-manifest persistence idempotently after a crash: align a
  checked-out integration branch after the ref CAS, rewrite external sources
  from the same desired manifest, and deduplicate the durable ledger receipt.
- Publish one manifest digest in coordination state: the canonical digest of
  `.syncwheel/manifest.json` on the recorded integration tip. Additive compose
  binds that digest and compares public topology snapshots structurally.
- Derive the global manifest-write transaction from one statically checked
  saver registry, including repository authority and coordination compose.
  Every parser command declares its behavior in that registry, and the
  command table is a projection of it.
- Publish the updated control commit from `stack push` as well, so the
  integration ref advances whenever a stack push changes the manifest.
- Probe the integration checkout before the control ref CAS: unrelated
  uncommitted work there now refuses the command, names the checkout, the
  paths and the command to rerun, and leaves the ref where it was. When the
  same work appears after the CAS, the operation still finishes, aligns the
  control manifest alone, and warns instead of leaving a pending intent.
- Settle a pending control-manifest intent from any manifest writer, not only
  from `stack push`, `int rebuild` and `int push`, so a later `stack create`
  can no longer strand the delivery commands. A source that already carries a
  newer proposal is kept: the interrupted operation is receipted when its
  control commit is already on the ref, and `int rebuild --reason` abandons it
  otherwise.
- Accept a modified `.syncwheel/manifest.json` as rebuild input, so the
  reviewed-proposal remedy named by a cross-clone divergence refusal can
  actually run in the checkout that carries the divergence.

## 0.42.0 - 2026-09-02

- Project Agentwheel revision-provider commits authored on a declared
  integration projection onto that exact integration tip, recording the
  integration-first base explicitly on the resulting draft stack instead of
  replaying conflicting lock changes against `origin/main`.
- Expire manifest-invalidated pending provider receipts with one local ledger
  event and the named remedy to run a new Agentwheel update; projection
  conflicts now name both their paths and the base used.
- Add manifest v3 `integration.derived_paths` and a verified Agentwheel
  revision-provider route decision. A candidate whose product blobs reproduce
  from the manifest base remains a draft stack; an allowed lock-only delta that
  does not reproduce is a trailer-marked derived integration commit, never a
  stack, draft ref, manifest mutation, or publication.
- Classify only trailer-marked derived commits, require every declared
  integration stack to be present, bind derived receipt recovery to the ordered
  integration composition, and make expiration journal-first and ledger
  idempotent. Conflict diagnostics use Git's name-only merge-tree output.
- Make the persisted route and hook-validated candidate immutable across
  recovery, keep manifest-base provider stacks landable despite their
  manifest-only control commit, and return one terminal expiry result from
  `check`, `preflight`, `finalize`, `recover`, and `release` as applicable.
- Report `derived-projection-stale` with affected paths after rebuild, preserve
  `derived_paths` through active-active coordination, and parse ownership only
  from Git's actual trailer block. Accepted cost: a derived lock does not reach
  `main` through Syncwheel until a later update qualifies for the
  `manifest-base` route.
- Route add, edit, and deletion by resulting blob equality, including an empty
  projection, and read every Git path list as NUL-delimited data without
  stripping significant line feeds or spaces.
- Bind each derived commit to a second `Syncwheel-Derived-Paths` content trailer
  and durable operation/commit/path provenance. Active-active snapshots carry
  that provenance so fresh peers retain `derived-projection-stale` after a
  rebuild; a mode-`0600`, atomically replaced and fsynced Git-common-dir store
  carries unpublished provenance across linked worktrees, while the ledger is
  an audit projection only.
- Keep narrowed or empty `integration.derived_paths` loadable and report the
  affected commits and paths as `derived-paths-narrowed`. Its named `int
  rebuild --reason 'reconcile narrowed derived paths'` remedy drops excluded
  derived commits and reconciles their provenance without disabling status,
  planning, or integration push inspection.
- Reject manifest-v2 coordination snapshots carrying derived provenance just
  like local manifest loading, and turn a partial revision-provider journal
  without valid `productPathObjects` into a named release-and-update recovery
  error.
- Treat the published coordination snapshot as the derived-provenance source and
  the Git-common-dir store as a local cache of unpublished records. A cache entry
  the snapshot has moved past is reported as `derived-provenance-diverged` by
  `validate`, `status`, and `plan` and then ignored, instead of failing every
  command that loads the manifest; two peers publishing the same declared path
  set no longer leave either of them without a usable command.
- Add `syncwheel coordination provenance reset --reason`, which discards the
  superseded clone-local records, and `--all`, which clears an unreadable store,
  both recording a `derived_provenance_reset` ledger event. Every provenance
  write now rebinds to the snapshot observed at that moment, and collects the
  temporary files a crash between write and rename would leave behind.
- Document that the common store is neither cloned nor pushed: without
  coordination a second clone sees the derived commit as unmapped, the landing
  guard does not fire there, and the provider refuses that clone.

## 0.40.2 - 2026-09-02

- Reap a registered lane with a missing path when its lease expired or its local
  owner PID is dead, regardless of its old configured-root location; retain a
  recovery ref for every existing lane-branch tip before removing the registry
  record, including a tip equal to the lane base.
- Honour `syncwheel_worktree_root` when opening a governed worktree lane.
- Add dry-run-first `worktree release <lane> --reason <why>` with explicit
  `--apply`, recovery refs, registry removal, and ledger evidence; existing
  dirty worktrees remain protected and name their recovery remedy.
- Let `gc --apply` reap eligible expired governed lanes even when active-active
  coordination is disabled.
- Resolve a moved lane through Git's current branch worktree before expiry
  cleanup, preserve dirty or locked worktrees, and report a clear dirty-race
  remedy if state changes immediately before removal.
- Restrict automatic lane cleanup to commands that are actually applying a
  mutation, while keeping previews read-only; worktree-creating `stack git` and
  `int git` forms are explicitly included.
- Make recovery-ref creation, pending cleanup, and ledger append retries
  idempotent, preserving release event type and reason after interruptions, and
  make `gc` preview and apply enumerate the same pending categories.
- Make governed-lane cleanup lock-first: acquire a tokenized Git worktree lock,
  verify its exact admin-dir and `gitdir`, persist retry intent, then anchor and
  delete refs through expected-old operations before touching the worktree.
- Probe tracked and untracked state immediately before removing only the
  verified registration, retain reappearing or changed paths, and never use a
  global worktree prune; interrupted removal and ref conflicts remain
  retryable through their recovery ref.
- Verify that the ref transaction reports `commit: ok`, accept the documented
  release retry for `branch_advanced`, and cover non-UTC lease expiry plus every
  lock, ordering, persistence, and targeted-cleanup invariant with
  mutation-sensitive tests.
- Recover a clone-local governed-worktree registry lock after `SIGKILL` when its
  owner is dead, reused, or zombie, and recover an empty or truncated lock after
  a short initialization grace; retain the atomically renamed stale inode and a
  durable recovery log.
- Fsync a cleanup-intent ledger event before refs, save registry generations by
  durable pre-image-digest CAS, and reconstruct an interrupted cleanup from the
  ledger even if the registry rolls back.
- Keep cleanup intents and terminal events selected by stack create, add, or
  capture in the effective external or personal manifest ledger.
- Make the real automatic `branch_advanced` state retryable through both `gc
  --apply` and an explicit release that re-anchors the new tip, make completed
  releases idempotent after a lost response, and select GC candidates under the
  registry lock so a reused lane id cannot yield a null-code failure.
- Answer `worktree release` with the terminal another concurrent Syncwheel
  command already wrote for the lane, instead of an unknown lane, and record the
  operator's `--reason` as a `governed_worktree_release_noted` ledger event
  whenever the terminal carries a different one.
- Let `worktree release` complete every pending reap state, not only
  `branch_advanced`, keeping the terminal type and reason of the intent already
  fsynced for it.
- Report an uninitialized registry lock recovery as such rather than as a stale
  owner, and prune retained stale lock inodes nobody holds during the next
  cleanup, keeping the recovery log as the durable evidence.

## 0.40.1 - 2026-09-02

- Pass the managed ref move handshake to shell replay steps, so plumbing
  rebuilds and `reconcile --apply` are no longer refused by the
  `reference-transaction` guard that told the user to run exactly those
  commands.

## 0.40.0 - 2026-09-02

- Add an optional `authority` block to the manifest so a repository can declare
  how far agents may take a change without a human gate: `human-gated` or
  `ai-managed`, with `source_change` and `runtime_change` as grantable classes
  and `destructive_rewrite` always denied. Absent means `human-gated`; the block
  is validated on load, never injected or enabled automatically, and stays out
  of coordination state.
- Add `syncwheel repo authority status` and `syncwheel repo authority set`
  (dry-run by default, `--apply` to write) and report the policy in
  `repo tracking status` and `status --json`.

## 0.39.1 - 2026-09-01

- Name manifest-derived capture or queue commands whenever an occupied primary
  checkout, a governed-worktree capacity limit, or an expired lane stops the
  workflow, rather than leaving agents to reconcile work manually.

## 0.39.0 - 2026-09-01

- Add plan-first `stack classify-integration` for manifest-only ownership of an
  integration commit. The operation requires an exact plan digest, updates no
  refs or worktrees, and preserves the commit in later cherry-pick and
  `merge-stacks` integration projections without materializing it on the stack
  source branch.

## 0.38.1 - 2026-09-01

- Guard the base branch and every stack landing target in the `pre-push` hook.
  A raw `git push origin HEAD:main` used to go through while the same push to
  the integration branch was refused; only `stack land` may publish there now.

## 0.38.0 - 2026-09-01

- Add explicit, clone-local governed worktree lanes through `syncwheel worktree
  open`, with a four-lane capacity, owner and lease records, configured-root
  containment, optional `--full` mode, and no automatic fallback from the
  primary checkout.
- Retain unfinished lane state visibly, recover committed clean lanes to a local
  recovery ref before reaping them, and expose machine-readable diagnostics plus
  terminal warnings for dirty, expired, pending, or unknown governed lanes.

## 0.37.1 - 2026-09-01

- Resolve the worktree path through `syncwheel_worktree_root` in `stack rebuild`,
  `int rebuild`, and `--auto-worktree`. They went straight to the hardcoded
  sibling layout, so a repository declaring a root still got worktrees dropped
  next to it while only `reconcile` honoured the setting.

## 0.37.0 - 2026-09-01

- Add a `reference-transaction` guard that refuses to rewind a manifest-managed
  branch onto a commit that does not contain its current tip, closing the gap
  where a plain `git reset --hard` silently dropped committed work that
  `pre-commit` and `pre-push` never observe.
- Resolve the ref directly when Git reports a zero old value, so a rewind
  performed with `git branch -f` is not mistaken for a branch creation.
- Authorize Syncwheel's own rebuilds per spawned Git process rather than through
  its ambient environment, and fail the hook open so a missing interpreter or an
  unreachable `syncwheel` can never block an ordinary commit.

## 0.36.3 - 2026-08-31

- Publish a local-ahead manifest-only integration control commit instead of
  normalizing it back to an older remote projection with the same product tree.

## 0.36.2 - 2026-08-31

- Allow a governed revision-provider handoff when `main-integration` is ahead
  of coordinated state only through manifest-only control commits.
- Treat declared stack commits already contained by the canonical base as
  absorbed instead of replaying them into artificial lock-file conflicts.
- Accept integration projections that differ only by the tracked Syncwheel
  manifest while continuing to reject unprojected product changes.
- Use the same absorbed-stack and manifest-control semantics for reconcile
  reports and the final coordinated publication gate.

## 0.36.1 - 2026-08-31

- Treat patch-equivalent replayed commits as satisfying stack and integration
  projections, while still rebuilding branches whose base or projected tree is
  genuinely stale.

## 0.36.0 - 2026-08-30

- Add a strict Agentwheel revision-provider protocol with exact path and hash
  leases, clean-checkout gating, and deterministic operation ownership.
- Validate product and manifest-only control commits through repository hooks
  before compare-and-swap ref updates, without publishing remote state.
- Add crash-safe, idempotent recovery for product commits, draft ownership, and
  control state, including audited no-commit and no-delta outcomes.
- Lease the physical Git index and pinned base ref across every provider phase,
  snapshot typed direct and symbolic ref identity across hook and final checks,
  and make ledger records and checkpoints fsync-backed with deterministic
  incomplete-tail recovery.
- Reject managed-branch base aliases, normalize bounded error responses, and
  recover only provably operation-owned `index.lock` files after fatal process
  interruption.
- Preserve typed symbolic-ref leases without dereference collisions and require
  bounded manual cleanup for unprovable Git 2.43 ref locks after process-group
  termination, before every recovery shortcut or terminal response.
- Restrict revision-provider bases to direct leased refs or exact commit SHAs,
  reject revision syntax, abbreviated hashes, and symbolic refs, and persist the
  owned stack on the immutable peeled base commit.
- Report the projected `draftTipSha` separately from the integration control
  commit, with all-or-none terminal draft ownership fields.

## 0.35.2 - 2026-08-30

- Converge the required repository hook bundle before every normal repo-aware
  Syncwheel command, including status and validation, while preserving explicit
  hook lifecycle commands, `local-only` clones, and reasoned clone-local disables.

## 0.35.1 - 2026-08-29

- Add `check --strict` as a deterministic readiness gate for validation
  warnings and non-empty reconciliation plans while preserving the observational
  exit behavior of plain `check`.
- Detect undeclared commits on local and publication-remote stack branches, and
  report published local/remote branch misalignment.
- Isolate each personal manifest's append-only ledger and replay checkpoint from
  the shared manifest and other personal operators.

## 0.35.0 - 2026-08-29

- Add a fast-forward coordination repair backend that binds the complete
  recorded-to-observed commit interval, both endpoint trees, and all guarded
  refs into a reviewed plan before appending state-only evidence.
- Refuse non-descendant histories, intervals above 1024 commits, ownership or
  plan uncertainty, state lease loss, and managed-ref drift without updating
  the managed branch.

## 0.34.9 - 2026-08-28

- Reject persistent desk replay for an empty stack before refs, worktrees,
  ledger events, or manifest state can mutate; keep automatic and plumbing
  replay available for empty stacks.
- Define desks as conflict-resolution or isolated validation surfaces for
  non-empty materialized stacks, and direct routine authoring and validation to
  the integration checkout.
- Initialize the source Syncwheel skill metadata at version 1.0.

## 0.34.8 - 2026-08-22

- Add a tree-equivalent coordination repair backend that proves identical
  managed-ref trees, binds every observed ref into the reviewed plan, and
  appends only coordination evidence under an exact state-ref lease.
- Stop fail-closed on different content, ownership uncertainty, state lease
  loss, or managed-ref drift before or immediately after the state CAS; the
  existing `github-lock` backend remains unsupported and non-mutating.
- Add digest-bound `coordination compose` for one additive local stack proposal
  derived from a known append-only base state. It preserves independently added
  remote stacks and unmapped integration commits while atomically publishing
  only the new stack ref and an append-only partial state child.

## 0.34.7 - 2026-08-22

- Treat the declared journal branch as the compliant primary checkout, so the
  managed checkout and commit guards do not apply delivery integration rules to
  a journal repository.

## 0.34.6 - 2026-08-22

- Bootstrap a missing remote journal branch with an exact absence lease, while
  retaining the existing fail-closed exact lease for an already-published ref.

## 0.34.5 - 2026-08-21

- Allow `resume` and `reconcile` to select their requested replay mode when
  their parser does not expose legacy `--in-place` or `--worktree` fields.

## 0.34.4 - 2026-08-21

- Accept a published stack ref rebuilt onto an advanced declared base only when
  Syncwheel can reproduce the exact deterministic replay of the prior declared
  commits. Divergent source content, ownership changes, and topology changes
  remain blocked by the managed-ref guard.

## 0.34.3 - 2026-08-21

- Compare the raw persisted manifest after an in-place replay, while retaining
  normalized validation for the manifest transaction. This accepts an unchanged
  tracked manifest whose omitted defaults are normalized only in memory.

## 0.34.2 - 2026-08-21

- Defer reconciliation manifest persistence until every requested stack and
  integration replay succeeds, so a tracked manifest cannot make its own
  in-place integration rebuild fail.
- Verify the exact manifest state materialized by an in-place replay before
  allowing the final atomic manifest write.

## 0.34.1 - 2026-08-21

- Recognize a single-parent integration commit whose patch is already reachable
  from the declared integration base as absorbed delivery evidence during
  reconciliation, instead of requiring manual ownership recovery.

## 0.34.0 - 2026-08-20

- Add `stack land`: a plan-first direct-landing path for a declared stack that
  validates exact source and integration projections, dependencies, clean
  worktrees, delivery observation, active-active alignment, required local
  checks or verifier-bound attestations, and a digest-bound exact lease.
- Keep PR promotion explicit: policy, check, merge-conflict, linear-history,
  or remote-rejection stops return the `stack promote` route and never create a
  pull request automatically. Landing supports fast-forward or deterministic
  two-parent merge candidates, durable operation receipts, and observation-only
  retry reconciliation.

## 0.33.4 - 2026-08-20

- Preflight every local reconciliation rebuild or alignment target before the
  first mutation. A dirty later target now stops `reconcile --apply` and its
  `sync` and `publish` wrappers without changing an earlier stack, manifest,
  or ledger.

## 0.33.3 - 2026-08-20

- Extend repository hooks with a primary-checkout guard: `post-checkout` reports
  branch drift immediately and `pre-commit` blocks commits outside the declared
  integration branch while feature worktrees remain allowed.
- Automatically install or upgrade the reversible hook bundle before mutating
  Syncwheel operations in required `git-tracked` clones; keep status and
  validation read-only and explicit about missing, stale, or tampered hooks.

## 0.33.2 - 2026-08-16

- Add plan-first `hooks status|install|remove` lifecycle for a composable managed-ref pre-push guard.
- Derive protected integration, stack/draft source, channel, coordination-state, historical owned, and journal refs dynamically.
- Scope publisher bypass to a short-lived, single-use remote/refset authorization and route every Syncwheel publisher through it.
- Document the local `--no-verify` limitation and GitHub ruleset/App hardening path.
- Make the guard required-by-default for owned `git-tracked` clones, with visible
  compatible migration, fail-closed mutation enforcement after activation, and a
  persisted reason-required opt-out.
- Permit fail-closed partial adoption of real new stack refs without rebuilding
  integration when integration shape and existing membership are unchanged.

## 0.33.1 - 2026-08-16

- Add plan-first `coordination repair` with a serialized reviewed plan, exact
  coordination-state CAS, append-only byte-preserving child state, ownership
  and pending-merge stops, idempotency, and post-application verification.
- Require a continuous external write freeze or transactional backend for
  apply; reject GitHub branch locks as unsupported because administrators can
  bypass or change them concurrently.

## 0.33.0 - 2026-08-15

- Add manifest version 3 deployment channels: ordered, pinned stack
  compositions materialized as rebuildable Git branches without treating branch
  publication as proof of an environment deployment.
- Add plan-first channel create, add, remove, replace, refresh, promote,
  channel-local resolve, apply, publish, and close lifecycles with deterministic
  plan digests, durable operation intent, terminal receipts, and
  observation-only reconciliation.
- Publish channel refs with exact leases and active-active coordination state;
  reject stale plans, changed refs, conflicts, and unknown outcomes instead of
  silently replaying them.
- Add shared and explicitly expiring ephemeral channels, exact dependency and
  stack-pin provenance, deterministic diff and inspection output, and bounded
  ledger evidence for channel operations.
- Document the difference between stacks, the full integration branch, pinned
  channels, and deployment-provider state across the CLI, agent contract, and
  public site.

## 0.32.0 - 2026-08-14

- Add manifest `repository_mode: "journal"` with explicit branch, remote,
  include/exclude allowlists, maximum file size, and a default 30-minute interval.
- Add plan-first `journal status`, `journal snapshot`, `journal publish`, and
  Linux systemd user scheduler install/status/remove commands.
- Snapshot through a locked temporary index, reject sensitive or secret content,
  verify HEAD and file stability, avoid empty commits, and realign the real index
  without changing the worktree.
- Publish only from an unchanged observed remote parent with an exact lease and
  stop on remote-ahead, divergence, or lease loss without history surgery.

- Bring the published surface in line with the working model: the landing page, README, `llms.txt`,
  `AGENT.md` and the bundled `syncwheel` skill no longer present a dedicated worktree per branch as
  the way to work. They now describe a single checkout, automatic replay-mode selection, and draft
  stacks, and the skill documents `--replay-mode`, `stack create --draft`, `capture-integration`,
  `promote` and `demote`.

- **`auto` no longer creates a worktree.** The default replay mode now selects
  `plumbing` where Git supports `merge-tree --write-tree`, and falls back to
  `ephemeral` below that threshold; `in-place` is still used when the target
  branch is already the current one, and `desk` when it already has a worktree
  or you ask for one. Routine `stack rebuild`, `int rebuild` and
  `reconcile --apply` therefore stop leaving a worktree behind — the last one
  matters most, since it is where worktrees used to accumulate. `desk` remains
  available and becomes what its name says: a place someone deliberately chose
  to work, and `reconcile --apply --replay-mode desk` restores the previous
  behaviour when that is what you want. Selection is available at
  four levels, most specific first — the `--replay-mode` flag, `replay_mode` in
  the repo-local `.syncwheel/profile.local.json`, `defaults.replay_mode` in the
  manifest, then built-in `auto`. Use the new `syncwheel replay-mode` command to
  read or set the repo-local default. An unavailable mode makes `auto` fall
  back rather than fail. The chosen mode is now recorded in `plan --json` and in
  the `stack_rebuilt` and `integration_rebuilt` ledger events. `use <name>` now
  keeps the other keys in `profile.local.json` instead of replacing the file.
- Add `--replay-mode plumbing`: Git 2.38+ replays directly through
  `merge-tree --write-tree`, `commit-tree`, and one final `update-ref`, without
  creating a working tree. Git capability is detected at runtime; older Git
  releases use the existing ephemeral replay path. A plumbing conflict names
  its paths and stops with an explicit `--replay-mode desk` retry command. The
  target ref must not already be checked out.
- Publish draft source refs to the coordination remote. Under `active-active`
  coordination, `stack push` and `reconcile --push` now carry
  `refs/heads/syncwheel/draft/*` through the normal atomic leased publication,
  so a second clone can rebuild a draft from the manifest alone. Pushing a draft
  anywhere else — a target/forge remote, an overridden `--remote`, or any
  uncoordinated manifest — is still refused and names the state.
- Add the explicit `--replay-mode ephemeral` rebuild path. It replays in a
  detached temporary worktree, updates the real branch ref before ledger
  collection, and removes the worktree on both success and failure.
- Add `stack capture-integration` to assign integration-first commits to a
  stack, rebuild only that branch through the shared replay executor, and keep
  no capture worktree after completion. Unmapped integration diagnostics now
  offer capture into a new draft stack as the durable remedy.
- Introduce an internal replay plan/execution seam while preserving the current
  porcelain replay behavior and dry-run transcript byte-for-byte.
- Add a hermetic, clone-per-execution replay determinism harness covering
  replayed commit identity, moved bases, binary files, renames, file mode
  changes, merge rejection, and the explicit stop policy for empty commits.
- Make stack and cherry-pick integration replays reproducible by carrying each
  source commit's author and committer metadata, while disabling clone-local
  rerere and non-reproducible GPG signing for replay commands. The first rebuild
  after this release can rewrite existing replayed branch SHAs once to their
  stable deterministic values; unchanged later rebuilds are no-ops.
- Make `merge-stacks` integration merge metadata deterministic by deriving it
  from the tip of each merged stack.
- Add draft and published stack state with a backwards-compatible published
  default, coordination-safe draft state transfer, and derived publication state.
- Add materialized `stack create --draft`, `stack promote`, and `stack demote`
  lifecycle commands. Promotion transfers managed branch ownership through an
  explicit active-active coordination permission, publishes a tombstone for the
  retained draft ref, and reports an old-name reconcile worktree path instead
  of moving it. Draft publication is refused while draft rebuilds remain
  available.
- New manifests require every declared stack to participate in integration;
  `stack create` now includes it by default under that policy.
- Add `manifest require-integration` to preview and apply an explicit migration
  after absorbed or abandoned stacks are closed.
- Reject required-membership manifests that leave declared stacks outside
  `integration.stacks`.

## 0.22.1 - 2026-08-02

- Document lossless dirty-checkout relocation, patch-equivalent merge proof,
  metadata-first stack closure, and recovery retention through verified delivery.

## 0.22.0 - 2026-07-31

- Require the primary Git worktree to remain on the manifest integration branch while allowing
  feature commands from dedicated worktrees; block status, validation, planning, checks, and
  handoff when the invariant is broken.

- Harden the Syncwheel skill with managed-repo detection, post-merge
  housekeeping guidance, squash-merge verification, and a housekeeping design
  spec.
- Default Syncwheel-managed worktrees to repo-relative `.syncwheel/wt/` while
  preserving explicit `var/syncwheel` manifest settings.
- Clarify that feature PRs deliver to their intended branch, never to
  `main-integration`, and document the post-merge stack cleanup flow.

## 0.21.3 - 2026-07-31

- Derive the checked-in GitHub Pages version labels from the root `VERSION`
  file, update them through the pre-commit release flow, and reject drift in CI.

## 0.21.2 - 2026-07-27

- Preserve canonical and publication remote identity in public coordination
  state without exposing local aliases, and reject unassigned remote aliases.
- Validate typed remote references at the remote-state boundary before handoff,
  race classification, or merge acceptance can consume them.

## 0.21.1 - 2026-07-27

- Prevent a tombstoned branch that becomes active again from becoming a local
  GC candidate, and require the tombstone's original remote tip before cleanup.
- Let closed-stack worktree locks be released by stack ID and revalidate GC
  candidates immediately before local deletion.
- Keep local remote aliases and remote-qualified local refs out of public
  coordination state while preserving each checkout's local transport config;
  retain canonical versus publication remote roles in schema 2 and reject
  ambiguous aliases.

## 0.21.0 - 2026-07-24

- Add manifest version 2 active-active coordination with a persisted disabled
  mode, explicit migration, and safe defaults for new `git-tracked` manifests.
- Publish managed refs and append-only remote coordination state through one
  atomic, exact-lease protocol; fail closed when atomic push is unavailable or
  a stale manifest would erase a remotely published stack.
- Add `handoff`, `coordination init`, `coordination disable`, `gc`, local
  worktree locks, and tombstone-backed `stack close` commands.
- Classify concurrent publication races as equivalent, explicitly mergeable
  disjoint stack changes, or conflicts; require `publish --accept-merge` for
  a reviewed merge.
- Add public protocol documentation and temporary-bare-remote coverage for
  atomic rejection, ownership, partial publications, legacy compatibility,
  privacy-safe state, tombstones, locks, and local cleanup.

## 0.20.0 - 2026-06-14

- Add short CLI aliases for common repo, manifest, JSON, dry-run, reconcile,
  push, remote, worktree, tracking, stack, integration, and self-update options.
- Add `spoke` as a readable CLI alias for `stack`.
- Document the short-flag map for agent and human workflows.

## 0.19.0 - 2026-06-13

- Add repo-local `syncwheel_tracking` policy for `git-tracked` and
  `local-only` Syncwheel manifest setup.
- Add `syncwheel repo tracking status` and `syncwheel repo tracking set` for
  inspecting and migrating manifest tracking modes.
- Default Syncwheel-managed worktrees to repo-relative `var/syncwheel/` through
  `syncwheel_worktree_root`.

## 0.18.0 - 2026-06-10

- Add uv packaging with a `syncwheel` console script while preserving direct
  `python3 scripts/syncwheel.py ...` execution.
- Add an idempotent `scripts/install.sh` for production uv installs and
  editable development installs.
- Extend `self status`, `self check-update`, and `self update` to distinguish
  git checkouts, uv tool installs, and plain script execution.
- Teach uv tool installs to check the upstream `VERSION` file directly and
  update with uv.
- Add CI coverage for editable and git-sourced uv tool install modes.

## 0.17.0 - 2026-05-13

- Add a segmented append-only ledger under `.syncwheel/ledger/` with a replayed
  checkpoint for cross-machine recovery state.
- Record manifest saves, stack rebuilds/pushes, and integration rebuilds/
  alignments/pushes into the ledger.
- Add `ledger show` to inspect the current replayed ledger state.
- Teach `resume` to restore previously known historical stacks from the ledger
  when ownership is deterministic and the historical branch still exists.

## 0.16.1 - 2026-05-13

- Remove Jira-specific stack auto-creation from `resume` so the recovery flow
  stays tracker-agnostic.
- Keep `resume` conservative: it now auto-registers only commits with exactly
  one already-detected owner and leaves all other cases in manual review.

## 0.16.0 - 2026-05-13

- Add `reconcile --mode resume` and the top-level `resume` command for
  cross-device recovery flows.
- Let `resume` auto-register unmapped integration commits on a deterministic
  owning stack.
- Allow integration rebuild/alignment to proceed from the primary checkout when
  that checkout is dirty only because of untracked `.syncwheel/` metadata.

## 0.15.0 - 2026-05-07

- Add commit-level guidance for unmapped integration commits in `check` and
  `reconcile` output.
- Include changed files, containing branches, likely stack owners, related
  declared commits with matching subjects, and suggested next commands.
- Add JSON diagnostics under `diagnostics.unmapped_integration_commits` for
  automation and tests.

## 0.14.0 - 2026-05-05

- Add top-level `sync` and `publish` lifecycle commands.
- Make safe local-to-remote alignment the default for `reconcile --apply`,
  `sync`, and `publish` when local and remote both match the manifest
  projection, with `--no-align-local-to-remote` as the escape hatch.
- Improve reconcile plan wording for remote projection alignment, local
  projection publishing, unassigned integration commits, and manual review
  cases.

## 0.13.2 - 2026-05-05

- Make `stack add` validate integration-first commits immediately.
- Reject commits made on top of a stale integration projection before mutating
  the manifest, and validate the updated stack projection before saving.

## 0.13.1 - 2026-05-05

- Stop writing the fallback `Syncwheel <syncwheel@example.com>` identity into
  target repository Git config during projection worktrees.
- Respect normal Git identity resolution for commit-creating commands and emit
  a yellow warning before using the Syncwheel fallback identity only when
  `user.name` or `user.email` is missing.

## 0.13.0 - 2026-05-05

- Add `stack absorb` for integration-first workflows where changes are made on
  the integration branch and then moved into the owning stack branch.
- Support pathspecs, `--staged`, default amend behavior, `--no-amend` with a
  custom commit message, and worktree creation/reuse for the target stack.
- After a successful absorb, update the manifest commit list and remove the
  absorbed patch from the integration checkout.

## 0.12.1 - 2026-05-05

- Show `git status --short --branch` in `reconcile` output before validation
  and drift sections so dirty working trees are explicit.
- Include `working_tree_status` and `working_tree_dirty` in `reconcile --json`
  output.

## 0.12.0 - 2026-05-04

- Add `reconcile --align-local-to-remote` for history normalization when local
  and remote branches both match the manifest projection but still differ by Git
  history.
- Keep that normalization explicit so normal `reconcile` remains a content
  no-op in the both-valid case.
- Add regression coverage for stack and integration branches with diverged
  history and identical projected trees.

## 0.11.1 - 2026-05-04

- Fix `reconcile` no-op detection when rewritten local and remote histories
  already match the manifest projection by tree but do not contain the exact
  historical manifest SHAs.
- Prevent validation SHA-containment drift from forcing rebuilds when
  `local_matches_projection` is already true.
- Add a regression test for diverged commit history with the same projected
  tree.

## 0.11.0 - 2026-05-04

- Make `reconcile` converge stale local managed branches to remote refs when
  those remote refs already match the manifest projection.
- Avoid regenerating new replacement SHAs, updating the manifest, or pushing
  again in the normal multi-device case where another device has already
  published the correct projection.
- For `merge-stacks` integration projections, let `reconcile` evaluate remote
  stack refs that already match the manifest so stale local stack branches do
  not cause false integration rebuilds.

## 0.10.0 - 2026-05-04

- Make `reconcile --push` use `--force-with-lease` by default, matching the
  normal multi-device lifecycle for rebuilt managed branches.
- Add `reconcile --no-force-with-lease` as the explicit escape hatch for normal
  Git pushes.

## 0.9.1 - 2026-05-04

- Add explicit `--force-with-lease` support to `reconcile --push`, `stack push`,
  and `int push` so the common rewritten-branch publish path does not require
  remembering Git passthrough syntax.
- Keep Git passthrough after `--` available for advanced push flags.

## 0.9.0 - 2026-05-04

- Add top-level `reconcile` / `rec` as the preferred multi-device maintenance
  workflow for manifest-owned stacks and integration branches.
- Report stack and integration drift against local branches, remote refs, and
  manifest-projected trees.
- Support dry-run-by-default planning, explicit `--apply`, optional `--push`,
  worktree-root rebuilds, stack filtering, publication remote override, and
  manifest SHA refresh after stack rebuilds.
- Add tests for reconcile planning and apply behavior with an external
  manifest.

## 0.8.2 - 2026-05-04

- Add `self install-hooks` so any Syncwheel clone can install the tracked Git
  hooks with a standard Syncwheel command.
- Report hook activation state in `self status`.

## 0.8.1 - 2026-05-04

- Add a tracked pre-commit hook for the version-bump guard.
- Add staged-file mode to `scripts/check-version-bump.py` so local hooks can
  reject commits before they are created.

## 0.8.0 - 2026-05-04

- Add `int sync-status` to compare local integration, remote integration, and
  the manifest-projected integration tree.
- Add `int align-remote` to backup and reset a clean shared integration checkout
  to its remote only when the remote matches the manifest projection, unless
  explicitly forced.
- Add `manifest compare` to inspect different integration compositions and
  identify shared, divergent, and composition-only stacks.
- Add end-to-end Git tests covering shared-integration remote alignment and
  multi-manifest comparison.
- Add a version-bump guard so release-relevant CLI changes must update
  `VERSION`, `CHANGELOG.md`, and the README current-version line.

## 0.7.2 - 2026-05-02

- Publish the detached-head update-detection fix as a new release version so pinned installs can verify the notifier behavior against a newer tagged version.

## 0.7.1 - 2026-05-02

- Detect available updates for detached-head and submodule-style syncwheel installs.
- Reuse existing target worktrees more safely during rebuilds.
- Clarify detached-install update detection in the docs.

## 0.7.0 - 2026-05-02

- Add built-in self update commands: `self status`, `self check-update`, and
  `self update`.
- Add automatic per-install update policy with `self mode off|notify|auto`.
- Emit visible update notices on normal syncwheel usage so human operators and
  AI agents do not silently keep using an outdated checkout.

## 0.6.0 - 2026-04-30

- Make `main-integration` the default shared integration branch created by
  `init`.
- Update the documented default operating model so day-to-day combined work
  happens on the integration branch and `main` remains the promotion branch.

## 0.5.1 - 2026-04-30

- Document `init` as the default manifest bootstrap command; keep `--stdout`
  as an advanced piping option.

## 0.5.0 - 2026-04-30

- Add repo-local profile selection with `use <profile>` and `use --shared`.
- Resolve `.syncwheel/profile.local.json` automatically when no explicit
  manifest or personal profile is passed.

## 0.4.0 - 2026-04-30

- Add `check`/`ck` as a single fetch + validate + plan command for the common
  inspection flow.
- Add short aliases for common commands (`st`, `v`, `pl`, `s`, `i`, `s new`,
  `s rb`, `i rb`, `g`) and `-p` for personal manifests.
- Add `SYNCWHEEL_REPO` and `SYNCWHEEL_PERSONAL` environment defaults so host
  projects can provide concise wrapper commands.

## 0.3.0 - 2026-04-30

- Add `init --personal <name>` to create ignored local manifests under
  `.syncwheel/manifests/<name>.local.json`.
- Add `stack create` so stack entries can be created without hand-editing the
  manifest.
- Document command-first manifest and stack creation flows for humans and AI
  agents.

## 0.2.0 - 2026-04-30

- Replace the previous materialization UI with the object/action CLI:
  `stack ...` and `int ...`.
- Add `stack sync`, `stack set`, and `stack add` so commit lists do not need to
  be edited by hand.
- Add `stack rebuild` and `int rebuild`, with worktree mode, `--in-place`, and
  `--dry-run`.
- Add `stack push` and `int push` wrappers around `git push`, including
  passthrough arguments after `--`.
- Add `stack git` and `int git` wrappers for running arbitrary Git commands in
  the target branch worktree.
- Add `integration.strategy: "merge-stacks"` for merge-shaped integration
  branches.
- Create automatic backup branches before rebuilding existing targets.
- Document the worktree-first model and human command recipes.

## 0.1.0 - 2026-04-29

- Initial manifest-driven status, validation, plan, and deterministic branch
  rebuild workflow.
