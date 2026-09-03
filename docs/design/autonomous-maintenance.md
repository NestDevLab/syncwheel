# Syncwheel Autonomous Maintenance

Status: implementation specification

Audience: implementation agents, reviewers, and maintainers

Primary implementation file: `scripts/syncwheel.py`

Primary test files: `tests/test_coordination.py`, `tests/test_syncwheel.py`, and
`tests/test_managed_ref_guard.py`

## 1. Purpose

This document specifies a portable, lossless, fail-closed maintenance path for
Syncwheel-managed delivery repositories. The implementation must let an agent
repair mechanically provable state/evidence drift and explicitly declared
derived projections as an ordinary part of an already authorized repository
task. It must not require a separate human approval that merely says "repair
Syncwheel".

The design deliberately avoids a required GitHub App, GitHub branch lock,
hosted coordinator, database, daemon, or provider-specific transaction API.
The correctness core uses Git objects, Git remote-ref observation, atomic Git
publication for refs that actually change, and compare-and-swap (CAS) on the
coordination state branch. Forge integrations may enrich evidence, but the
generic Git proof must remain sufficient for every `SAFE_AUTO` action.

This is an implementation specification, not a description of possible
directions. The normative words **MUST**, **MUST NOT**, **SHOULD**, and **MAY**
have their RFC 2119 meanings. An implementation agent must follow the batches
and acceptance criteria in this document and must not substitute a simpler raw
Git workflow.

## 2. Product outcome

After this design is implemented and a repository opts into safe autonomous
maintenance, an agent performing a normal repository task must be able to run:

```bash
syncwheel maintain
```

The command must do one of three things:

1. apply only digest-bound, mechanically proven, lossless maintenance and
   return a verified receipt;
2. observe that no maintenance is needed and return an idempotent no-op
   receipt; or
3. stop without an unsafe mutation and return the exact semantic decision or
   unique-data risk that requires a human.

The command must never respond with a generic instruction to "resolve
manually" when it can instead name a machine-readable classification and a
specific blocked object.

The normal working model must be:

- the primary checkout remains on `manifest.integration.branch`;
- product work is owned by a declared draft or published stack;
- stack branches are materialized by plumbing or a self-removing ephemeral
  worktree;
- a persistent `desk` worktree exists only for an explicit conflict-resolution
  or isolated-test need;
- `main-integration` is a reproducible Syncwheel-owned derived projection of
  the current delivery base plus the ordered active stacks;
- merged stacks are closed before integration is rebuilt;
- unowned integration commits are never silently dropped;
- raw pushes to managed refs remain guarded.

"Single branch" in this contract means a single routine working branch and
checkout. Separate stack refs still exist because Git forges require a source
ref for independent pull requests. Agents must not routinely check out those
stack refs.

## 3. Non-goals

The implementation must not:

- merge a pull request, deploy software, or mutate a delivery environment;
- infer product intent for unowned commits;
- resolve overlapping stack edits or stack-order conflicts;
- delete a dirty worktree, unique commit, stash, unmerged branch, recovery ref,
  or remote branch;
- make a forge adapter part of the correctness boundary;
- weaken the managed-ref guard or recommend `--no-verify` as recovery;
- use `git reset --hard`, rebase, force-push, or raw branch deletion as a
  generic repair;
- treat `git push --atomic` with an unchanged refspec as proof that an
  unchanged ref was leased;
- claim that a state-only observation CAS freezes other managed refs;
- publish a stack/source ref, delivery target, or other normal application ref
  as part of `maintain`;
- rewrite a parent coordination state commit;
- automatically enable autonomous maintenance in an existing repository;
- create routine persistent worktrees;
- put manifest-maintenance commits into the same manifest revision as normal
  product-stack commits.

Provider-side enforcement, immutable archival refs, and remote branch deletion
may be designed later. They are not required for this change.

## 4. Existing implementation map

The implementation agent must read the following symbols before editing. Line
numbers are intentionally omitted because this repository is active; symbol
names are the stable anchors.

| Concern | Existing symbol | Required relationship |
|---|---|---|
| Manifest loading | `load_manifest` | Normalize the new maintenance policy here. |
| Manifest persistence | `save_manifest`, `save_manifest_with_ledger` | Reuse atomic manifest writes and ledger recording. |
| Portable public snapshot | `coordination_manifest_snapshot` | Include the shared maintenance policy. |
| Snapshot localization | `apply_coordination_snapshot` | Preserve or adopt the shared maintenance policy. |
| Manifest merge | `merge_coordination_snapshots` | Keep maintenance policy in shared globals. |
| State validation | `validate_coordination_state` | Validate optional maintenance evidence. |
| State loading | `read_remote_coordination_state` | Reuse exact state-tip observation and fetch behavior. |
| Remote refs | `remote_ref_tips` | Use for every remote preflight and postflight. |
| Ownership conflicts | `coordination_ownership_conflicts`, `require_exclusive_coordination_ownership` | Must pass before any automatic state CAS. |
| State construction | `build_coordination_state` | Reuse for ordinary coordinated publication. |
| Repair child | `build_coordination_repair_state` | Do not overload arbitrary repair; add a separate maintenance child builder. |
| State commit | `create_coordination_state_commit` | Reuse for append-only maintenance children. |
| Current global drift check | `coordination_state_matches_remote` | Make tombstone-aware; do not weaken active-ref checking. |
| Publication preflight | `validate_coordination_publication_base` | Invoke maintenance classification before returning the current generic stale-state error. |
| Atomic publisher | `coordinated_publish` | Keep for actual managed-ref changes. |
| State merge | `merge_coordination_snapshots` | Reuse only when changes are provably disjoint. |
| Stack close | `command_stack_close` | Factor reusable close planning and content-merge proof. |
| Integration projection | `materialize_integration_projection`, `integration_sync_report`, `command_int_rebuild` | Reuse; do not create another projection engine. |
| Stack projection | `materialize_stack_projection`, `stack_reconcile_report` | Reuse for rebuild classification. |
| Reconcile | `reconcile_actions`, `command_reconcile` | Reuse action execution after maintenance converges control state. |
| Local GC | `coordination_gc_plan`, `run_coordination_gc` | Reuse and strengthen recoverability/dirtiness checks, including backups. |
| Guard policy | `managed_push_guard_policy`, `ensure_managed_repository_hooks` | Verify/bootstrap before a mutating maintain apply. |
| CLI parser | `build_parser` | Register `maintain` and the maintenance-policy command. |
| Canonical digest | `canonical_json_digest` | Use for plans, evidence, and receipts. |

The implementation must not introduce a second module solely for aesthetic
reorganization in the first delivery. Keep the initial code in
`scripts/syncwheel.py`, following the existing single-file architecture. A
future refactor may extract modules after behavior is covered by tests.

## 5. Current failure model

The current active-active state stores a normalized manifest snapshot and a
`managed_refs` map. `coordination_state_matches_remote` requires every recorded
tip to equal the current remote tip. `validate_coordination_publication_base`
therefore blocks every publisher when any historical observation becomes stale.

This is correct for unknown active content, but too coarse for three provable
cases:

1. **Equivalent active ref replacement.** The remote active ref has a different
   commit ID but the exact same tree as the recorded commit. No product content
   changed.
2. **Merged stack ref already absent.** A stack source ref was removed after its
   patches were absorbed by its declared delivery target. The correct action is
   to close the stack and record a tombstone, not recreate the branch.
3. **Local manifest strictly behind published state.** The remote state owns
   stack records absent locally, while the local checkout has no competing
   control-plane change. The correct action is exact remote snapshot adoption,
   not a new declaration invented locally.

The managed-ref guard makes these liveness gaps visible because it prevents the
old unsafe workaround: a raw push that changes a managed ref without publishing
matching coordination state.

## 6. Safety model

### 6.1 Separate topology authority from transport evidence

The coordination state's `manifest` and `manifest_digest` remain authoritative
for shared topology: stack identity, branch ownership, stack order, channel
composition, integration configuration, and coordination identity.

`managed_refs` is the last accepted transport evidence for owned refs. A
maintenance child may update this evidence only when an action-specific proof
defined in this document succeeds. An evidence update must not be treated as a
server-side lock or as proof that the ref could not move after observation.

This distinction removes the need for a provider-specific continuous freeze for
the narrow equivalence cases. A maintenance action that only appends state never
overwrites application content. If an external actor changes a ref after the
pre-CAS observation, post-verification reports `RETRYABLE_DRIFT`; a later
maintenance run observes again. No unique code is reset, deleted, or blessed as
equivalent without proof.

The existing general `coordination repair` command remains unchanged and still
requires a verified freeze/backend for arbitrary tip adoption. `maintain` is not
a replacement for arbitrary repair. It handles only the proofs enumerated in
Section 9.

### 6.2 Authority classes and linearization boundaries

`maintain` has four authority classes. The plan and receipt MUST name the class
of every action; an implementation MUST NOT present a derived update as a
state-only repair or as ordinary source publication.

**State/evidence maintenance**

- Changes only the coordination state ref and, when necessary, the local
  tracked manifest.
- May bootstrap a recognized Syncwheel-owned managed-hook bundle as the local
  guard prerequisite; a foreign hook chain remains a blocker.
- Uses exact CAS on the state ref.
- Does not send a refspec for an unchanged managed application ref.
- Does not claim a lease on unchanged application refs.
- Re-observes all guarded refs immediately after CAS.
- On any post-CAS drift, ends the current attempt before projection, publish,
  or cleanup. It may make a bounded fresh state-only plan only after recording
  the accepted child and re-observing from scratch.
- Never rolls the append-only child back.

**Owned derived-projection maintenance**

- Is limited to refs explicitly listed in the versioned maintenance policy and
  proven to be Syncwheel-owned derived projections.
- Version 1 permits only `manifest.integration.branch` (normally
  `main-integration`); a stack/source branch, delivery target, channel, or
  arbitrary managed ref is never a derived projection in this mode.
- Rebuilds the local projection only after all source stack tips, ordered
  composition, ownership, recoverability, and calculated projection digest
  match the plan.
- Creates an append-only recovery ref before moving an existing local derived
  ref, then publishes only that derived ref through `coordinated_publish` with
  its exact old-tip lease and the state ref atomically.
- Stops fail-closed on drift, lease loss, ownership uncertainty, unowned
  integration content, or any failed proof.

**Source/code publication**

- Publishes stack/source refs or delivery targets as part of the normal delivery
  operation selected by the repository task.
- Is never an action in `syncwheel maintain`, regardless of the maintenance
  policy. `coordinated_publish` remains the shared implementation primitive,
  but maintenance may call it only for an eligible owned derived projection.

**Local-artifact cleanup**

- Deletes only local worktrees, branches, or backups already proven
  recoverable.
- Is excluded by default. It may run automatically only when the persistent
  maintenance policy explicitly enables local reap, and never deletes a remote
  ref.

### 6.3 Standing authority

Add one shared manifest policy. The initial opt-in deliberately makes the
derived authority explicit and keeps destructive cleanup disabled:

```json
"maintenance": {
  "mode": "safe-auto",
  "derived_projection_refs": ["main-integration"],
  "local_reap": "manual"
}
```

Policy fields:

- `mode`: `manual` produces a plan but does not apply it without explicit
  `--apply --plan-digest`; `safe-auto` permits digest-bound state/evidence
  actions under an otherwise authorized repository mutation task;
- `derived_projection_refs`: an ordered, duplicate-free list of branch names
  permitted for owned derived-projection maintenance. Version 1 accepts either
  an empty list or exactly `manifest.integration.branch`; any other value is
  rejected rather than interpreted as source-ref authority;
- `local_reap`: `manual` (default) or `safe-auto`. Only `safe-auto` authorizes
  automatic local cleanup after all recoverability checks pass.

If the block is absent, normalize it as:

```json
{"mode": "manual", "derived_projection_refs": [], "local_reap": "manual"}
```

These are stable authority classes, not arbitrary per-action booleans. The
safe-action set and the permitted derived ref remain part of the versioned
Syncwheel contract and test suite.

Existing manifests must remain manual until a one-time explicit migration:

```bash
syncwheel manifest maintenance safe-auto --derived-projection integration
syncwheel manifest maintenance safe-auto --derived-projection integration --apply --plan-digest <digest>
```

`integration` is the sole version-1 CLI selector and resolves to the exact
normalized `manifest.integration.branch`; the command accepts no arbitrary
branch name for this authority.

The dry-run prints the old policy, new policy, manifest path, and digest. Apply
must require that exact digest through `--plan-digest`. The policy change is a
control-plane manifest edit and must be delivered according to the existing
manifest self-reference rule.

The standing policy authorizes safe Syncwheel maintenance only inside an
otherwise authorized repository mutation task. The generated digest is a
machine precondition, not a human token to be copied on every run: safe-auto
must regenerate it and reprove every condition at apply time. The policy does
not authorize an agent to invent background product work, publish source/code,
merge PRs, deploy, or mutate a repository during a read-only user request.

## 7. Classifications

Every maintenance plan has one top-level classification.

### 7.1 `NOOP`

All of the following are true:

- manifest and state topology are aligned;
- active managed refs match accepted evidence;
- expected-absent tombstoned refs are absent or are correctly recognized as
  reused active refs;
- every local projection is convergent or no local mutation was requested;
- no merged active stack needs closure;
- no policy-authorized local artifact needs reaping;
- the primary checkout and managed hook bundle are compliant.

The command exits zero and emits a receipt. A second run against unchanged state
must produce the same plan digest and another no-op result.

### 7.2 `SAFE_AUTO`

Every planned action belongs to the allowlist in Section 9, its authority class
is enabled by the normalized policy, all required observations are exact,
ownership is unambiguous, and no action can destroy or overwrite unique
content. A source/code publication action is never `SAFE_AUTO`.

Under `maintenance.mode=safe-auto`, the command may apply immediately. Under
`manual`, it prints the plan and exits with the plan-required code.

### 7.3 `RETRYABLE_DRIFT`

The plan was safe, but one of its exact preconditions moved between planning,
apply, or post-verification. This includes state CAS loss and post-CAS guarded
ref drift.

The command must:

- preserve any append-only child already accepted;
- avoid retrying an uncertain application-ref mutation;
- re-observe from scratch;
- retry at most `MAINTENANCE_MAX_RETRIES`, fixed initially at `2`;
- use a new plan digest for every changed observation;
- return a receipt for each accepted state child;
- stop with exit code `4` after the retry bound.

No human approval is needed to re-observe or retry a state-only action.

### 7.4 `HUMAN_DECISION_REQUIRED`

At least one semantic or destructive ambiguity exists. The command must not
apply any remote mutation. It may retain already completed local read-only
observations. It must print structured blockers with exact refs, stack IDs,
commit IDs, paths, and reasons.

Mandatory blocker reasons include:

- `unknown-active-ref-content`;
- `overlapping-stack-change`;
- `stack-order-conflict`;
- `unowned-integration-commits`;
- `ambiguous-merge-proof`;
- `dirty-worktree`;
- `unique-local-branch`;
- `ownership-conflict`;
- `manifest-local-control-delta`;
- `unsupported-state-schema`;
- `unknown-remote-observation`;
- `projection-conflict`;
- `channel-dependency-blocks-close`;
- `stack-dependency-blocks-close`;
- `guard-chain-conflict`.

The human question must describe the semantic choice. It must not ask for a
generic Syncwheel repair authorization.

## 8. CLI contract

### 8.1 `syncwheel maintain`

Add this parser next to `handoff`, `gc`, `sync`, and `publish`:

```text
syncwheel maintain [-r REPO] [-M MANIFEST] [-p PERSONAL]
                    [-F|--no-fetch] [-j|--json]
                    [--plan-only]
                    [--apply --plan-digest DIGEST]
                    [--max-retries {0,1,2}]
```

Rules:

- Default behavior depends on the normalized maintenance policy.
- `manual` plus no `--apply` is plan-only.
- `safe-auto` plus no `--plan-only` applies a `SAFE_AUTO` plan.
- `--plan-only` always prevents mutation.
- Explicit `--apply` always requires `--plan-digest`, even in `safe-auto` mode.
- Bare safe-auto internally binds apply to the digest it just generated; it
  still performs the full apply-time regeneration and comparison.
- `--no-fetch` prohibits Git fetch but still permits `ls-remote`; it may make
  proofs impossible. Missing objects under `--no-fetch` produce
  `HUMAN_DECISION_REQUIRED` with reason `proof-object-unavailable`, not an
  invented conclusion.
- local reap is selected solely by the persisted `maintenance.local_reap`
  policy; a CLI flag must not silently widen or narrow that repository
  authority.
- `--max-retries` may lower the fixed bound but must not exceed `2` in the first
  release.
- The command supports delivery repositories only. Journal mode must reject it
  and direct the caller to `journal status`/`journal publish`.

### 8.2 Exit codes

Use stable exit codes:

| Code | Meaning |
|---:|---|
| `0` | Verified no-op or verified successful maintenance. |
| `1` | Existing manifest/local validation error unrelated to a classified safe action. |
| `2` | CLI usage or malformed plan. Preserve existing top-level error behavior. |
| `3` | Valid plan exists but policy/manual mode requires explicit apply. |
| `4` | Retryable drift exceeded the bounded retry count or ended uncertain. |
| `5` | Human semantic decision is required. |

Do not use exit code `0` when blockers are present merely because JSON was
printed.

### 8.3 Output modes

Human output must lead with:

```text
maintenance: NOOP|SAFE_AUTO|RETRYABLE_DRIFT|HUMAN_DECISION_REQUIRED
plan digest: <sha256>
```

JSON output must emit the full plan in plan-only mode and a receipt containing
the plan in apply mode. Error text may accompany JSON on stderr, but stdout must
remain valid JSON.

## 9. Safe action specifications

Only the following actions may be classified `SAFE_AUTO` in the first release.
The action type strings are part of the public JSON contract.

### 9.1 `install-managed-hooks`

Preconditions:

- manifest tracking is `git-tracked`;
- the guard policy says required and not disabled;
- `managed_hook_bundle_status` reports `absent` or Syncwheel-owned stale files;
- no foreign hook-chain collision exists;
- the install preflight has an exact expected digest.

Apply:

- call the existing managed hook bootstrap;
- do not replace an unrecognized hook;
- verify every installed wrapper and chained-hook digest.

Postcondition:

- `managed_push_guard_policy(...).ready` and `.enforced` are true.

A foreign or modified chain is `guard-chain-conflict`, not safe-auto.

### 9.2 `adopt-remote-manifest`

Purpose: resolve a local manifest that is strictly behind the published state
without inventing declarations.

Preconditions:

1. An active-active remote state exists and validates.
2. The local manifest file still has its planning digest.
3. The published state's manifest version is compatible with the local
   manifest according to `read_remote_coordination_state`.
4. `coordination_manifest_snapshot(local)` differs from the remote snapshot.
5. The local checkout has no uncommitted change to the shared manifest.
6. The plan names a `knownBaseStateTip` and `knownBaseSnapshotDigest`; the
   locally stored, validated state at that exact tip has the named snapshot
   digest, and the normalized local manifest equals that snapshot exactly.
7. The current remote state is a validated descendant of `knownBaseStateTip`,
   and its snapshot is a strict extension of the base with no changed shared
   global, stack, channel, landing, or maintenance record.
8. Every remote-added stack has an unambiguous branch owner and an exact
   recoverable remote tip.
9. No local-only stack metadata other than `meta` and local remote aliases would
   be lost. `apply_coordination_snapshot` already preserves these fields and
   must remain the conversion primitive.

The first implementation should add:

```python
def classify_remote_manifest_adoption(repo_root, manifest, manifest_path, state_info):
    ...
```

It returns either a complete action or a blocker. It must not call
`merge_coordination_snapshots` when there is no common base. Strict-behind
adoption is not the same as accepting a disjoint concurrent merge. Absence of
the exact validated base state is `manifest-local-control-delta`, not a reason
to infer that the local manifest is behind.

Apply:

```python
updated = apply_coordination_snapshot(manifest, state['manifest'])
save_manifest_with_ledger(
    repo_root,
    manifest_path,
    updated,
    'maintenance_adopt_remote_manifest',
    context={
        'state_tip': expected_state_tip,
        'known_base_state_tip': known_base_state_tip,
        'known_base_snapshot_digest': known_base_snapshot_digest,
        'before_digest': local_manifest_digest,
        'after_digest': manifest_digest(updated),
    },
)
```

Before saving, re-read the manifest transaction fingerprint and remote state
tip. Any change invalidates the plan.

This action does not push the manifest commit. It changes the tracked working
tree as control-plane metadata. The caller's normal repository delivery task
must commit/publish that scoped manifest change separately under the existing
self-reference rule.

### 9.3 `accept-tree-equivalent-ref`

Purpose: update stale state evidence when an active managed ref has a different
commit ID but identical content.

Preconditions for ref `R`:

1. `R` is owned by exactly one active coordination domain.
2. Parent state records exact commit `A` for `R`.
3. Current remote observation is exact commit `B` for `R`.
4. `A != B`.
5. Both commit objects are locally available after the command's allowed fetch.
6. `ref_tree(repo_root, A) == ref_tree(repo_root, B)`.
7. `R` is still declared by the parent snapshot, or it is the integration ref
   described by that snapshot.
8. No manifest topology change is combined unless separately classified safe.
9. All other owned refs are observed exactly and included in the plan.

Commit ancestry is informative but not required. Tree identity is the proof.
A patch-equivalent but tree-different ref is not safe-auto in this action.

Plan evidence:

```json
{
  "type": "accept-tree-equivalent-ref",
  "ref": "refs/heads/integration/shared",
  "recordedTip": "<40-hex A>",
  "observedTip": "<40-hex B>",
  "recordedTree": "<40-hex tree>",
  "observedTree": "<same 40-hex tree>",
  "proof": "exact-tree-equality"
}
```

Apply builds one append-only maintenance child. It updates
`managed_refs[R] = B`, sets `changed_refs = {}`, and adds:

```json
"maintenance_evidence": {
  "schemaVersion": 1,
  "planDigest": "<digest>",
  "classification": "SAFE_AUTO",
  "observations": [
    {
      "type": "tree-equivalent-ref",
      "ref": "refs/heads/integration/shared",
      "beforeTip": "<A>",
      "afterTip": "<B>",
      "tree": "<tree>"
    }
  ]
}
```

The child uses the parent state commit as its sole Git parent and its
`parent_state` value. `publication_scope` is `maintenance:state-evidence`.

The command pushes only:

```text
<child-state-commit>:<state-ref>
```

with:

```text
--force-with-lease=<state-ref>:<expected-parent-state-tip>
```

Do not include a no-op refspec for `R`. Do not claim that `R` was leased. After
CAS, observe the state ref and every guarded ref. If `R` moved again, record the
accepted child receipt, classify the overall run `RETRYABLE_DRIFT`, and end the
attempt immediately. It must not persist a derived projection, publish any ref,
or reap local artifacts from that attempt.

### 9.4 `close-absorbed-missing-stack`

Purpose: close a stack whose remote source branch is already absent after its
patches were absorbed into the declared delivery target.

Preconditions for stack `S`:

1. `S` exists in the published state manifest.
2. `S` has an exact historical managed tip in parent `managed_refs`.
3. The live source ref is absent on the stack's publication/coordination remote.
4. The delivery ref is resolved from `target_remote` and `target_branch`, not
   from `main-integration`.
5. The delivery tip is observed live and its commit object is available.
6. Every non-merge stack commit is patch-absorbed by the delivery tip.
7. No active channel references `S`.
8. No active stack declares `depends_on: S`.
9. The local manifest does not contain a competing modification of `S`.
10. The stack's historical tip and commit list remain recoverable from at least
    one of the local object database, delivery history, integration history, a
    remaining remote ref, or a named recovery ref. If not, closure may still be
    logically correct but local cleanup is forbidden; classify a recoverability
    blocker for the cleanup action.

#### Generic Git merge proof

Implement:

```python
def stack_absorption_proof(repo_root, stack, delivery_tip):
    ...
```

Rules:

1. If every stack commit is an ancestor of `delivery_tip`, return
   `exact-ancestry`.
2. Otherwise run patch equivalence for each non-merge stack commit. Existing
   `commit_patch_id` may be reused, but the implementation must compare against
   the delivery range from the stack base to `delivery_tip` and must reject
   missing/ambiguous patch IDs.
3. Equivalently, `git cherry <delivery-tip> <stack-tip>` may be used if and only
   if the exact stack tip is available and every output row for a stack-owned
   commit is `-`. Any `+` row blocks closure.
4. A raw tree diff between delivery and stack tip is not sufficient because
   delivery may contain unrelated later work.
5. Merge commits without a deterministic per-parent patch proof block safe-auto
   unless ancestry already proved them absorbed.

Plan evidence includes delivery remote role, delivery ref, delivery tip,
historical stack tip, all checked commit IDs, and the proof result. Do not store
credentials or a raw remote URL.

Apply:

- derive the updated manifest by removing `S` from `stacks` and
  `integration.stacks`;
- create a tombstone with the historical recorded tip even though the live ref
  is absent;
- preserve every other parent-state field;
- update the state manifest snapshot and digest to the updated topology;
- keep the historical `managed_refs` entry for recoverability;
- add maintenance evidence with `proof=patch-absorbed-and-source-absent`;
- CAS only the state ref;
- re-observe the state ref and every guarded ref immediately after CAS; any
  drift ends the attempt before local manifest persistence or projection work;
- save the same updated local manifest only after the accepted state CAS and
  immediate guarded-ref post-check;
- if local manifest persistence fails after remote CAS, report an explicit
  partial result and let the next run adopt the exact remote snapshot.

The updated state must treat the absent tombstoned ref as a valid observation.
Change `coordination_state_matches_remote` so that:

- an active ref must still equal its recorded active tip;
- a tombstoned ref that is not reused by the active manifest may be absent;
- a tombstoned ref that still exists must equal the tombstone's `remote_tip`;
- a reused ref is active and must follow the active exact-tip rule;
- any other mismatch remains false.

Factor this logic into:

```python
def coordination_ref_expectations(state):
    """Return {ref: {'presence': 'required'|'optional-tombstone', 'tip': sha}}."""

def coordination_state_remote_comparison(repo_root, config, state):
    """Return structured matches/mismatches instead of a bare boolean."""
```

Keep `coordination_state_matches_remote` as a compatibility boolean wrapper.

### 9.5 `rebuild-owned-derived-projection`

Purpose: restore the local `manifest.integration.branch` projection after
control state is convergent. This is derived-projection maintenance, not source
publication.

Preconditions:

- manifest adoption/state maintenance has completed;
- `manifest.integration.branch` is the only branch listed in
  `maintenance.derived_projection_refs` in version 1;
- the branch is exclusively owned by the active coordination domain and is
  declared derived by the integration projection contract;
- all commit objects required by the projection exist;
- target worktree, if any, is clean;
- no unique local target tip would be lost without a recovery ref;
- replay planning succeeds without a content conflict;
- every active stack tip, ordered stack list, integration base, and calculated
  projection digest equal the plan evidence;
- `integration_commit_diagnostics` reports zero unowned non-control commits;
- primary checkout remains compliant.

Apply:

- reuse `replay_plan`, `select_replay_mode`, and `execute_replay`;
- default to plumbing;
- use an ephemeral worktree only when plumbing is unavailable;
- never select `desk` automatically after a conflict;
- create the existing backup ref before moving an existing local branch;
- append the existing stack/integration rebuilt ledger event;
- do not push until the rebuilt integration projection validates exactly.

A replay conflict is `projection-conflict`. The receipt must include the exact
`--replay-mode desk` command, but `maintain` must not create that worktree.

### 9.6 `publish-owned-derived-projection`

Purpose: publish only the eligible Syncwheel-owned integration projection after
all control-state maintenance and local validation have succeeded.

Preconditions:

- the sole changed application ref is `manifest.integration.branch`, listed by
  `maintenance.derived_projection_refs` and exclusively owned by the active
  coordination domain;
- the local integration ref equals its materialized projection and expected
  composition digest;
- `main-integration` contains exactly the declared ordered projection;
- there are zero unowned integration commits;
- the normal coordinated publication base validates against the just-maintained
  state;
- exact expected old tips are still current.

Apply:

- call `coordinated_publish` once with only the owned derived integration ref;
- do not implement a second publisher inside `maintain`;
- preserve current atomic push and exact-lease behavior;
- create or retain the append-only local recovery ref before any local move;
- on lease loss, uncertainty, or post-publication drift, stop fail-closed and
  use the existing race classification without a blind retry.

`maintain` MUST NOT call this action for a stack/source ref, delivery target, or
any ref not listed as an eligible derived projection. Those publications remain
normal delivery operations outside maintenance mode.

### 9.7 `reap-policy-authorized-local-artifact`

Purpose: remove routine worktree/branch/backup accumulation without risking
local data.

Preconditions and execution must reuse `coordination_gc_plan` and
`run_coordination_gc`. Additionally:

- `maintenance.local_reap` is exactly `safe-auto`; otherwise the planner may
  report candidates but MUST NOT include this action;
- a worktree must be clean, non-current, unlocked, and inside the declared
  Syncwheel root;
- every candidate worktree, branch, or backup tip must be recoverable from
  current remote state, a tombstone, a delivery ref, or a distinct retained
  recovery ref;
- dirty, conflicted, submodule-blocked, or externally located worktrees are
  skipped and reported;
- no remote branch is deleted;
- backup retention remains controlled by `coordination.gc`, but elapsed age or
  count alone never proves a backup deletable;
- a missing inactive tombstoned ref is a valid remote observation. Its historic
  tip remains required only for the independent local recoverability proof.

Governed lane cleanup uses a narrower lock-first protocol. The first cleanup
mutation is a tokenized `git worktree lock`; failure means the lane is in use
and no ref or path is changed. While holding that lock, Syncwheel verifies the
registration's exact admin-dir and `gitdir`, persists retry intent, anchors the
tip with expected-old protection, and commits an expected-old transaction that
verifies the recovery ref while deleting the lane branch. A final
tracked/untracked probe precedes removal of that registration only; global
worktree prune is forbidden. Ref conflict, path reappearance, registration
drift, or process death leaves a retryable record and an immutable recovery ref.

This guarantee covers concurrent Syncwheel commands and ordinary non-forced Git
operations. Raw changes to Syncwheel-owned refs, double-force operations that
bypass Git's worktree lock, and direct non-owner writes into the lane during
cleanup are outside the supported threat model; Syncwheel still fails closed
when it detects their effects.

## 10. Unsafe cases and exact questions

The following table defines the required human-facing question. The
implementation may add technical evidence but must preserve the decision being
asked.

| Blocker | Required question |
|---|---|
| Unowned integration commits | "Which existing or new draft stack owns commits `<ids>`?" |
| Overlapping stack snapshots | "Both local and published state changed stack `<id>`. Which topology should win, or should the changes be manually combined?" |
| Stack-order conflict | "Local and published state declare different integration order. Which ordered stack list is intended?" |
| Unknown active-ref content | "Remote ref `<ref>` contains tree `<tree>` that is neither the recorded tree nor the manifest projection. Who owns this content?" |
| Ambiguous merge proof | "Stack `<id>` is absent remotely, but commits `<ids>` are not provably absorbed by `<delivery-ref>`. Was it merged, abandoned, or accidentally deleted?" |
| Dirty worktree | "Worktree `<path>` contains uncommitted state and cannot be reaped or realigned. Should it be retained for recovery?" |
| Unique local branch | "Local branch `<branch>` has commits not recoverable from known remote/state refs. Which stack should own them?" |
| Ownership conflict | "Coordination domains `<ids>` both claim `<ref>`. Which domain owns it?" |

Never transform one of these into `--force`, `--accept-merge`, branch deletion,
or manifest rewriting without the corresponding explicit decision.

## 11. Plan schema

Add:

```python
MAINTENANCE_PLAN_SCHEMA_VERSION = 1
MAINTENANCE_EVIDENCE_SCHEMA_VERSION = 1
MAINTENANCE_RECEIPT_SCHEMA_VERSION = 1
MAINTENANCE_MAX_RETRIES = 2
MAINTENANCE_MODES = {'manual', 'safe-auto'}
```

The canonical plan shape is:

```json
{
  "schemaVersion": 1,
  "operation": "maintain",
  "classification": "SAFE_AUTO",
  "coordinationId": "default",
  "remote": "origin",
  "remoteIdentityDigest": "<sha256>",
  "objectFormat": "sha1",
  "stateRef": "refs/heads/syncwheel/state/default",
  "expectedStateTip": "<40-hex or null>",
  "knownBaseStateTip": "<40-hex or null>",
  "knownBaseSnapshotDigest": "<sha256 or null>",
  "manifestPath": ".syncwheel/manifest.json",
  "localManifestDigest": "<sha256>",
  "publishedManifestDigest": "<sha256 or null>",
  "policy": {"mode": "safe-auto"},
  "expectedRefs": {
    "refs/heads/integration/shared": "<40-hex or null>"
  },
  "actions": [],
  "blockers": [],
  "planDigest": "<sha256>"
}
```

### 11.1 Determinism

- Omit timestamps, random UUIDs, local absolute repo paths, and installation IDs
  from the plan.
- Store `manifestPath` relative to the repository root.
- Sort ref-map keys lexicographically.
- Sort actions by the execution ordering in Section 13, then stable action key.
- Sort blockers by reason and object key.
- Normalize nullable missing refs as JSON `null`.
- Bind the SHA-1 object format, known base state tip, and known base snapshot
  digest when an action depends on a published-manifest ancestry proof.
- Compute `planDigest` over the entire plan except `planDigest` itself using
  `canonical_json_digest`.
- Replanning unchanged state must yield the same digest.

`remoteIdentityDigest` is the SHA-256 of the credential-free canonical remote
identity. Add a helper that reads `git remote get-url`, strips userinfo from
HTTP(S) URLs, normalizes an SCP-like SSH URL to host/path, and hashes the result.
The raw URL must not appear in public state, plans, logs, or receipts.

### 11.2 Action common fields

Every action has:

```json
{
  "type": "<stable action type>",
  "authorityClass": "state-evidence|derived-projection|local-reap",
  "classification": "SAFE_AUTO",
  "objectKey": "<stable identifier>",
  "preconditions": {},
  "evidence": {}
}
```

An action containing an unrecognized key is allowed for forward-compatible
readers, but apply must reject an unrecognized action `type`.

### 11.3 Blocker fields

Every blocker has:

```json
{
  "reason": "<stable reason>",
  "objectType": "ref|stack|manifest|worktree|coordination",
  "objectKey": "<identifier>",
  "detail": "<human-readable detail>",
  "decision": "<specific question>"
}
```

## 12. Receipt schema

Every apply attempt, including accepted partial state-only work followed by
postflight drift, emits a receipt:

```json
{
  "schemaVersion": 1,
  "operation": "maintain",
  "operationId": "<sha256 derived from planDigest and attempt>",
  "planDigest": "<sha256>",
  "classification": "SAFE_AUTO",
  "status": "succeeded|noop|partial|retryable-drift|failed|unknown",
  "state": {
    "before": "<tip or null>",
    "after": "<tip or null>"
  },
  "refs": {
    "before": {},
    "after": {}
  },
  "actions": [
    {"type": "...", "objectKey": "...", "status": "succeeded|skipped|failed"}
  ],
  "verification": {
    "stateParentValid": true,
    "manifestDigestValid": true,
    "ownershipValid": true,
    "primaryCheckoutCompliant": true,
    "integrationProjectionValid": true,
    "guardReady": true
  }
}
```

The receipt may include an informational timestamp, but the timestamp is not
part of the plan digest. Persist the receipt as an append-only ledger event
`maintenance_receipt`. If a state child was created, store the plan digest and
compact evidence inside that child as specified above.

An interrupted or ambiguous remote outcome must be `unknown`, never silently
retried. State-only CAS can be reconciled by observing the state tip. A normal
multi-ref publication must use existing coordinated publication outcome rules.

## 13. Planner algorithm

Add these top-level functions near the existing coordination functions:

```python
def normalize_maintenance_policy(value, path='maintenance'):
    ...

def collect_maintenance_snapshot(repo_root, manifest, manifest_path, fetch=True):
    ...

def classify_remote_manifest_adoption(repo_root, manifest, manifest_path, snapshot):
    ...

def coordination_ref_expectations(state):
    ...

def coordination_state_remote_comparison(repo_root, config, state, observed_refs=None):
    ...

def stack_absorption_proof(repo_root, stack, delivery_tip):
    ...

def build_maintenance_plan(repo_root, manifest, manifest_path, fetch=True):
    ...

def validate_maintenance_plan(plan):
    ...
```

`collect_maintenance_snapshot` must collect, in this order:

1. Git object format and an explicit unsupported-format blocker when it is not
   SHA-1 in version 1;
2. manifest transaction fingerprint and canonical digest;
3. current branch and all worktree statuses;
4. managed hook bundle status;
5. coordination config and remote identity digest;
6. remote state tip and validated state;
7. the durable last-seen state tip, and its exact validated snapshot when an
   adoption proof needs it;
8. exclusive-ownership conflicts;
9. union of active manifest refs, published-state managed refs, tombstone refs,
   delivery target refs, and state ref;
10. one live `ls-remote` observation for that union, grouped per remote;
11. object availability for every commit needed by proofs;
12. local integration projection report;
13. GC candidates without applying them.

Do not call `ls-remote` separately for each ref when refs share a remote. Extend
or wrap `remote_ref_tips` to batch the union. Observation failure is a blocker;
do not treat it as branch absence.

The planner then applies this precedence:

1. malformed state, incompatible version, remote observation failure, or
   ownership conflict -> blocker;
2. dirty manifest/control-plane work -> blocker unless it is exactly the
   already planned remote adoption result;
3. classify strict remote manifest adoption;
4. classify every active/historical ref mismatch;
5. classify merged missing stacks before generic missing-branch rebuilds;
6. construct the effective manifest after safe control-state actions;
7. validate stack/channel dependencies against that effective manifest;
8. classify owned derived integration rebuild and publication only when the
   policy names exactly that branch and all derived proofs pass;
9. reject any requested source/code publication as outside maintenance mode;
10. classify local GC only when `maintenance.local_reap=safe-auto`;
11. derive the top-level classification;
12. canonicalize and digest.

If any blocker exists, top-level classification is
`HUMAN_DECISION_REQUIRED`. The first release must not partially apply unrelated
safe actions in the same plan when a blocker exists. This makes behavior easier
for weak executors to reason about.

## 14. Apply algorithm

Add:

```python
def build_coordination_maintenance_state(previous_state, previous_tip, effective_manifest,
                                         plan, installation):
    ...

def apply_maintenance_plan(repo_root, manifest, manifest_path, plan):
    ...

def postverify_maintenance(repo_root, manifest, manifest_path, plan, result):
    ...

def command_maintain(args):
    ...
```

### 14.1 Apply preflight

`apply_maintenance_plan` must:

1. validate the plan schema and digest;
2. resolve the repository, require `git rev-parse --show-object-format` to
   return `sha1`, and compare it to the plan `objectFormat`;
3. confirm its remote identity digest;
4. reload the manifest and compare `localManifestDigest`;
5. re-read the remote state and compare `expectedStateTip`;
6. when present, fetch and validate `knownBaseStateTip`, then compare its
   snapshot digest to `knownBaseSnapshotDigest`;
7. re-observe every `expectedRefs` entry and compare exact values, including
   absence represented by `null`;
8. rerun exclusive ownership checks;
9. rerun each action-specific proof from live objects;
10. verify primary checkout and worktree dirtiness;
11. ensure the managed hook bundle or safely install it if that action is in the
   plan;
12. acquire the existing local coordination lease.

Any mismatch invalidates the plan before remote mutation.

### 14.2 Execution order

Execute actions in this fixed order:

1. `install-managed-hooks`;
2. calculate effective remote/local manifest adoption in memory;
3. when an action changes state evidence or topology, build one combined
   append-only maintenance state child for those actions; a pure
   `adopt-remote-manifest` action creates no state child;
4. when a child exists, push it with exact state CAS;
5. after a state CAS, independently observe the accepted child and every
   guarded ref. On any drift, write a `retryable-drift` receipt, release the
   lease, and end this attempt before every remaining step. For pure adoption,
   re-read the unchanged expected state tip before persisting the local
   manifest;
6. persist the effective local manifest with transaction checks;
7. rebuild only the eligible owned derived integration projection;
8. run full validation and project-specific derived-projection checks;
9. publish only that eligible derived ref with the normal coordinated publisher;
10. independently re-read state and all guarded refs;
11. reap policy-authorized eligible local artifacts;
12. append the maintenance receipt;
13. release the local lease in `finally`.

If state CAS succeeds but local manifest persistence fails, do not attempt to
roll back state. Return `partial`, include the accepted state tip, and allow the
next run to perform `adopt-remote-manifest`.

If local derived-projection rebuild fails, preserve state and manifest. Return
failed with the replay evidence; do not publish any ref.

If owned derived-projection publication is uncertain, use its existing outcome
classification. Do not retry the push from `maintain`.

### 14.3 State child construction

When the plan contains a state-changing action, the maintenance child must:

- validate the parent state;
- have the parent state commit as sole Git parent;
- set `parent_state` to the same exact parent tip;
- preserve unmodified tombstones and managed-ref values;
- update only refs proven by actions;
- store the effective normalized manifest snapshot and digest if topology was
  safely adopted or a stack was closed;
- set `changed_refs={}` for observation-only actions;
- use `publication_scope='maintenance:state-only'`;
- set a new publication UUID, timestamp, Syncwheel version, and installation ID;
- include compact `maintenance_evidence` bound to the plan digest;
- pass `validate_coordination_state` before commit creation.

The state commit message should be:

```text
syncwheel: autonomous maintenance <short-plan-digest>
```

Do not include repository-private names in the generic message.

### 14.4 Post-verification

Post-verification is independent of in-memory apply state. It must re-read from
Git/remote and prove:

- the state ref outcome is known;
- the state commit has the expected sole Git parent;
- `parent_state` equals that parent;
- embedded `planDigest` equals the applied plan;
- manifest snapshot digest is valid;
- every active owned ref has a valid expected observation;
- missing tombstoned refs are accepted only by the tombstone-aware rule;
- no ownership conflict exists;
- local manifest equals the accepted state snapshot after localization;
- the sole requested derived integration branch matches the calculated tree and
  composition digest;
- zero integration commits are unowned;
- primary checkout is compliant;
- guard bundle is ready;
- no dirty local artifact was removed.

Only then may status be `succeeded`.

## 15. Integration and stack hygiene

Autonomous state maintenance alone is insufficient. The command must leave the
repository usable for normal agent work.

### 15.1 Definition of clean integration

`main-integration` is clean when:

1. its base is the current manifest integration base;
2. it contains every active `integration.stacks` member exactly in declared
   dependency/order semantics;
3. it contains no closed stack projection;
4. `integration_commit_diagnostics` reports zero unmapped non-control commits;
5. manifest-only control commits are classified by the existing self-reference
   rule and do not become product stack commits;
6. its materialized tree equals `materialize_integration_projection`;
7. the primary checkout is on that branch.

Do not define clean as `git status` alone.

### 15.2 Start-of-task AI procedure

The shipped Syncwheel skill must eventually tell agents:

1. Confirm the user's request authorizes repository mutation.
2. Run `syncwheel repo tracking status`.
3. Run `syncwheel maintain --json`.
4. Stop only on exit codes `1`, `4`, or `5`; do not ask for generic repair
   approval on exit `3` if the repository is intended to migrate to safe-auto.
5. Reuse an existing stack only when its purpose and scope match.
6. Otherwise create a draft before the first product commit:

   ```bash
   syncwheel stack create --draft <task-slug> --purpose "<bounded purpose>"
   ```

7. Work in the primary integration checkout.
8. Capture new integration commits into that draft immediately with
   `stack capture-integration`.
9. Never leave unowned product commits at handoff.

The first implementation does not need a new `work start` wrapper. The existing
draft/capture commands are sufficient once `maintain` provides convergence.
Avoid introducing extra CLI surface until usage proves it necessary.

### 15.3 End-of-task AI procedure

1. Capture all scoped product commits into the intended stack.
2. Run project-specific tests.
3. Run `syncwheel maintain --json` to rebuild and validate projections.
4. Publish through `stack push`, `publish`, or the action already selected by
   the normal repository delivery procedure.
5. Re-run `syncwheel maintain --json`.
6. Report stack ID, branch, integration result, receipt digest, tests, and any
   actual semantic blocker.

## 16. Portability boundary

### 16.1 Core Git interface

Version 1 supports repositories with Git's SHA-1 object format only. Before
planning, `maintain` MUST inspect `git rev-parse --show-object-format`; a
SHA-256 or unknown format returns the explicit
`HUMAN_DECISION_REQUIRED` blocker `unsupported-object-format` and performs no
mutation. Supporting SHA-256 is a later, cross-cutting Syncwheel change because
existing manifest and state validators currently use 40-hex object IDs.

Within that boundary, the safe-auto core may depend on:

- Git commit/tree/ref inspection;
- `git ls-remote`;
- ordinary fetch of named objects/refs;
- `git cherry`, patch IDs, ancestry, and tree IDs;
- atomic push capability for normal coordinated publication;
- exact force-with-lease on refs that are actually sent;
- state-only exact CAS;
- local filesystem atomic replace/fsync behavior already used by manifest
  persistence.

The core must not call `gh`, GitHub REST/GraphQL, GitLab APIs, or provider web
hooks.

### 16.2 Optional forge evidence

A future adapter may report PR state, merge commit, source-branch deletion, and
branch protection. Such evidence may improve messages or let the planner fetch
the right objects, but it must not by itself turn an otherwise unproven action
into `SAFE_AUTO`.

Adapter results must be marked informational unless the same action passes the
generic Git proof.

### 16.3 No server-side lock requirement for state evidence

The design intentionally does not implement a GitHub lock backend. State-only
safe maintenance never overwrites a managed code ref. Exact state CAS prevents
lost state history; post-observation detects moving application refs. This is a
different contract from arbitrary coordination repair, where accepting unknown
content as authoritative still requires continuous serialization.

## 17. Manifest normalization changes

In `load_manifest`:

```python
data['maintenance'] = normalize_maintenance_policy(data.get('maintenance'))
```

`normalize_maintenance_policy` must:

- accept `None` as manual;
- require an object when present;
- require `mode` to be `manual` or `safe-auto`;
- require `derived_projection_refs` to be an ordered duplicate-free array of
  branch names, defaulting to `[]`;
- require `local_reap` to be `manual` or `safe-auto`, defaulting to `manual`;
- after integration normalization, reject a non-empty
  `derived_projection_refs` value unless it is exactly
  `[manifest.integration.branch]` in version 1;
- reject unknown keys;
- return a new normalized dictionary;
- never mutate the supplied object.

In `coordination_manifest_snapshot`, include the normalized policy because it is
a shared authorization contract:

```python
'maintenance': dict(manifest['maintenance'])
```

In `snapshot_globals`, include `maintenance`. Therefore two devices cannot
silently merge different maintenance authority modes as disjoint stack edits.

In `apply_coordination_snapshot`, adopt `maintenance` from the published
snapshot. For legacy snapshots without it, normalize manual.

State validation must accept old snapshots without `maintenance` as the complete
manual policy and new snapshots only when all policy keys validate.

Do not bump the delivery manifest version solely for this additive block. The
state remains backward-readable. Older Syncwheel versions will ignore the
top-level manifest block but will fail closed on state drift as before; they
will not perform autonomous actions.

## 18. Tombstone-aware comparison

The current boolean comparison is insufficient. Implement structured output:

```json
{
  "matches": false,
  "refs": {
    "refs/heads/pr/example": {
      "expectation": "optional-tombstone",
      "expectedTip": "<sha>",
      "observedTip": null,
      "status": "expected-absent"
    }
  },
  "mismatches": []
}
```

Algorithm:

1. Build active refs from `state.manifest` stack, integration, and channel
   records.
2. Build tombstone map keyed by normalized full ref.
3. For every `managed_refs` key:
   - active wins over tombstone;
   - active expects exact recorded tip;
   - inactive tombstone permits absence or exact tombstone tip;
   - historical non-tombstoned ownership expects exact recorded tip and cannot
     be auto-removed.
4. Observe all refs in one remote call.
5. Return per-ref status and mismatch list.

`coordination_gc_plan` MUST consume the same expectations. For an inactive
optional tombstone, observed absence is `expected-absent`, not a reason to skip
the candidate. GC must then prove local recoverability from the tombstone's
historic tip and an independent remaining recovery location; it must never
infer recoverability merely from remote absence.

For a `delete_backup` candidate, configured retention makes the backup eligible
for evaluation but MUST NOT replace that proof. A backup whose tip is reachable
only through itself is retained, even after its retention age has elapsed.

This change must be used by:

- `coordination_state_matches_remote`;
- `classify_coordination_race`;
- `validate_coordination_publication_base`;
- `coordination_gc_plan` and its candidate recheck;
- `handoff` JSON diagnostics;
- maintenance planning and post-verification.

`handoff` should add `ref_comparison` to its coordination object. Keep existing
keys for compatibility.

## 19. Test specification

All tests use local bare Git remotes. No test may require network access or a
real forge. Use synthetic repository, branch, stack, and commit names.

### 19.1 New test file

Create `tests/test_autonomous_maintenance.py` with one fixture class modeled on
`ActiveActiveCoordinationTest`. Reuse helpers from existing tests only if doing
so does not require a test-support refactor larger than the feature. Duplicating
a small local fixture helper is preferable to moving unrelated test code in the
first PR.

### 19.2 Policy tests

1. Missing maintenance block normalizes to manual.
2. `safe-auto` round-trips with an explicit integration derived-ref list and
   manual local reap.
3. An empty derived-ref list is accepted; a stack/source branch or any branch
   other than `manifest.integration.branch` is rejected in version 1.
4. Unknown mode, local-reap mode, or maintenance key is rejected.
5. Policy is included in public coordination snapshot/digest.
6. Different authority policy values make snapshot globals conflict.
7. Remote snapshot adoption localizes and preserves policy.
8. Policy dry-run is deterministic and digest-bound.
9. Policy apply rejects manifest drift.

### 19.3 Plan determinism tests

1. Same repo observation produces byte-equivalent canonical plan data and the
   same digest.
2. Ref iteration order does not affect digest.
3. Timestamp and installation ID are absent.
4. Tampering any expected tip invalidates digest.
5. Executing a plan in a repository with another remote identity is rejected.
6. Unknown action type is rejected by apply.
7. Plan contains relative, not absolute, manifest path.
8. The plan binds `objectFormat`, `knownBaseStateTip`, and
   `knownBaseSnapshotDigest` when remote-manifest adoption is proposed.
9. A SHA-256 repository returns `unsupported-object-format` and performs no
   mutation.

### 19.4 Tree-equivalent ref tests

Build two commits with different parents/messages and the same tree.

1. Planner classifies exact tree equality as safe-auto.
2. State child changes only the selected `managed_refs` value plus evidence and
   identity fields.
3. `changed_refs` is empty.
4. Parent manifest, digest, tombstones, and other managed refs remain equal.
5. Apply sends only the state ref and exact state lease.
6. State CAS loss leaves remote state unchanged and returns retryable drift.
7. Ref movement before apply invalidates the plan.
8. Ref movement after state CAS preserves the child, returns retryable drift,
   and proves that manifest persistence, derived rebuild/publish, and local
   reap were not called in that attempt.
9. Tree-different commits are human-decision-required.
10. Missing recorded/observed objects block proof.
11. Replanning after successful adoption is no-op.
12. Generic `coordination repair` behavior and unsupported GitHub backend remain
    unchanged.

### 19.5 Missing merged stack tests

Create a stack, publish its state, squash or patch-equivalent merge it into the
delivery branch, then remove the remote stack ref outside Syncwheel.

1. `git cherry`/patch proof classifies the stack absorbed.
2. Plan closes the stack instead of planning branch recreation.
3. State CAS adds a tombstone with the historical tip.
4. Updated state snapshot and local manifest remove the stack and integration
   membership.
5. Missing tombstoned remote ref is a valid state match.
6. A second maintain is no-op.
7. A `+` cherry row blocks closure.
8. A merge commit without ancestry proof blocks closure.
9. Active channel reference blocks closure.
10. Dependent stack blocks closure.
11. Local competing stack edit blocks closure.
12. Missing remote observation due command failure is not treated as absent.
13. Local dirty worktree is retained and reported even when shared closure
    succeeds.
14. No remote branch deletion command is issued.
15. After grace, an absent optional tombstone is eligible for local cleanup only
    when a separate local recovery location is proven.

### 19.6 Strict remote manifest adoption tests

1. Remote state contains one additional stack and local manifest is unchanged
   from its known base: safe adoption.
2. Exact remote stack record, order, state, commits, and typed refs are adopted.
3. Local `meta` and remote aliases are preserved as defined by
   `apply_coordination_snapshot`.
4. Local modified shared stack plus remote additional stack is not strict-behind
   adoption.
5. Overlapping stack change blocks.
6. Changed integration order blocks.
7. Changed maintenance policy blocks disjoint auto-merge unless local is known
   strictly behind.
8. Manifest file drift after plan blocks apply.
9. Remote state drift after plan blocks apply.
10. State accepted but local save failure returns partial; next run adopts the
    remote state.
11. Missing, invalid, or non-ancestor `knownBaseStateTip` blocks adoption.
12. A local snapshot whose digest differs from `knownBaseSnapshotDigest` blocks
    adoption even when its stack set appears to be a subset.

### 19.7 Integration hygiene tests

1. After merged-stack closure, integration rebuild excludes the absorbed stack.
2. Remaining stacks stay in declared order.
3. Integration tree equals materialized projection.
4. Unowned integration commit yields exit code 5 and exact commit IDs.
5. Manifest-only control commit follows existing diagnostics.
6. Primary checkout mismatch blocks apply.
7. Plumbing rebuild leaves no new worktree.
8. Plumbing conflict does not create a desk worktree.
9. Existing dirty stack worktree blocks before any unsafe local move.
10. An opted-in `main-integration` derived ref publishes with its exact lease
    plus the state ref atomically.
11. A stack/source ref or delivery target is never proposed for publication by
    `maintain`, even when it is otherwise convergent.
12. Derived-ref lease loss stops fail-closed and is never blindly retried.

### 19.8 GC tests

1. Clean recoverable tombstoned worktree is eligible.
2. Dirty worktree is skipped.
3. Locked worktree is skipped.
4. Current primary checkout is skipped.
5. Worktree outside managed root is skipped.
6. Unique local branch is skipped.
7. Backup retention keeps configured count and age guarantees.
8. Manual local-reap policy reports candidates but performs no delete or
   `update-ref` operation.
9. `safe-auto` local-reap policy permits only the independently recoverable
   candidates; an absent optional tombstone is not treated as data loss.
10. An expired backup reachable only through itself is retained; it becomes
    eligible only after an independent recovery location is proven.

### 19.9 Guard and authorization tests

1. `safe-auto` maintenance routes state CAS through the managed publisher
   authorization mechanism where required.
2. Raw push remains blocked.
3. Single-use authorization cannot be replayed.
4. Authorization refset for state-only maintenance contains only the state ref.
5. Hook tamper stops before CAS.
6. Missing owned hook bundle can be bootstrapped by a safe action.
7. Foreign hook-chain collision is human-decision-required.
8. Derived-projection authorization contains exactly the declared integration
   ref and state ref, never a source/code ref.

### 19.10 Receipt and interruption tests

Inject failures at each numbered apply step in Section 14.

1. Failure before state CAS leaves state and manifest unchanged.
2. Interrupt after successful state CAS is reconciled from remote observation.
3. State CAS success plus manifest-save failure is partial.
4. Derived-projection failure after manifest save publishes no ref.
5. Derived integration publication rejection changes neither the integration
   ref nor state.
6. Unknown derived-projection publication outcome remains unknown and is not
   retried.
7. Receipt records all completed and skipped actions.
8. Receipt verification flags are independently recomputed.

## 20. Regression fixtures corresponding to observed failure classes

Use synthetic identifiers; do not put private repository names or operational
SHAs in tests or public docs.

### Fixture A: equivalent integration replacement

- Publish coordinated state recording integration commit `A`.
- Construct commit `B` with the same tree and a different parent/message.
- Move remote integration to `B` outside Syncwheel.
- Expected: safe tree-equivalent evidence adoption and state-only CAS. A later
  owned derived-projection rebuild is permitted only when the integration branch
  is explicitly listed by policy and all independent projection proofs match.

### Fixture B: merged source ref deleted

- Publish an active stack source ref and state.
- Patch-equivalent merge into delivery base.
- Delete source ref outside Syncwheel.
- Expected: safe stack closure/tombstone, no branch recreation, integration
  rebuilt without the stack only when the integration branch is explicitly
  opted into derived-projection maintenance.

### Fixture C: local manifest missing published stack

- Publish a state snapshot with stack `remote-only`.
- Restore local manifest to the immediately preceding unchanged snapshot.
- Keep all remote stack refs recoverable.
- Expected: strict remote snapshot adoption only when the exact known base state
  and snapshot digest are available, then no stale-manifest maintenance error.

These fixtures are mandatory acceptance tests because they represent distinct
causes and must not be collapsed into one generic "repair stale state" branch.

## 21. Implementation batches

Each batch must be a separate reviewable change. Do not begin the next batch
until the current batch's focused and full tests pass.

### Batch 1: classifier and policy, no automatic mutation

Files:

- `scripts/syncwheel.py`
- `tests/test_autonomous_maintenance.py`
- `tests/test_coordination.py` only for shared state-comparison coverage
- `README.md`
- `docs/design/autonomous-maintenance.md` if implementation reveals an approved
  correction
- `VERSION` and `CHANGELOG.md` according to the existing version-bump guard

Implement:

- maintenance policy normalization and manifest command;
- SHA-1 object-format gate;
- tombstone-aware structured state comparison;
- tombstone-aware GC candidate classification;
- plan schema/digest;
- all safe classifiers;
- `syncwheel maintain --plan-only` and JSON output;
- exit codes without apply behavior.

Validation:

```bash
python3 -m unittest tests.test_autonomous_maintenance
python3 -m unittest tests.test_coordination
python3 -m unittest tests.test_managed_ref_guard
python3 -m unittest discover -s tests
```

Batch acceptance: all three synthetic fixtures produce the expected safe actions
and every ambiguous variant produces a blocker. No remote mutation occurs.

### Batch 2: state-only apply and receipts

Files:

- `scripts/syncwheel.py`
- `tests/test_autonomous_maintenance.py`
- `tests/test_coordination.py`
- `tests/test_managed_ref_guard.py`
- `README.md`
- `docs/ai-agents.md`
- `VERSION` and `CHANGELOG.md`

Implement:

- maintenance state child builder;
- exact state CAS;
- apply preflight/postflight;
- bounded retry for state-only drift;
- immediate terminal stop after post-CAS drift;
- manifest persistence after state CAS;
- receipts and interruption reconciliation;
- explicit `--apply --plan-digest`;
- safe-auto internal digest binding.

Validation adds a command-trace assertion proving no application-ref refspec is
sent during state-only maintenance.

Batch acceptance: Fixtures A, B, and C converge without generic repair approval,
while tree-different/ambiguous cases remain blocked and lossless.

### Batch 3: owned derived projection and policy-authorized local hygiene

Files:

- `scripts/syncwheel.py`
- `tests/test_autonomous_maintenance.py`
- `tests/test_syncwheel.py`
- `docs/ai-agents.md`
- `docs/agent-procedure.md`
- `skills/syncwheel/SKILL.md`
- `README.md`
- `VERSION` and `CHANGELOG.md`

Implement:

- reuse of the integration projection action after control-state convergence;
- merged-stack suppression before missing-branch recreation;
- integration clean invariant;
- owned derived integration publication through the existing publisher with an
  exact lease;
- policy-gated local GC integration;
- explicit rejection of source/code publication from maintenance mode;
- final AI start/end procedure;
- user-facing blocker questions.

Batch acceptance: an opted-in derived integration run updates only the declared
integration ref, leaves source/code refs untouched, preserves explicitly
locked/dirty desks, reports zero unowned commits, and produces a no-op second
run. Local artifacts are reaped only under the persistent local-reap opt-in.

### Batch 4: controlled rollout

This is operational delivery, not part of the source implementation itself.
It requires separate authorization.

Steps per repository:

1. install/release the reviewed Syncwheel version;
2. run `maintain --plan-only --json` under manual policy;
3. archive the plan and review classifications;
4. run focused dry-run fixtures against a disposable local bare remote;
5. migrate policy to `safe-auto` with the exact derived integration ref and an
   explicit `local_reap` value through its digest-bound command;
6. commit and publish the manifest control-plane change through the normal
   delivery contract;
7. run `maintain` twice and require the second run to be no-op;
8. verify ordinary stack and integration publication;
9. roll out the updated Syncwheel skill through its owning source/fleet process;
10. do not hand-edit generated agent runtime directories.

No fleetwide repair is implied by source readiness.

## 22. Review checklist for a weak execution model

The execution model must check every box and include the evidence in its handoff.

### Before editing

- [ ] Confirm current path is the Syncwheel source repository.
- [ ] Run `syncwheel repo tracking status`.
- [ ] Run `git status --short --branch` and preserve unrelated changes.
- [ ] Read every existing function named in Section 4.
- [ ] Confirm no other change already implements `maintain`.
- [ ] Create an isolated Syncwheel stack/work lane according to the current
      repository contract only after mutation authority includes implementation.
- [ ] Do not edit generated runtime directories.

### During implementation

- [ ] Add constants and normalizers before command code.
- [ ] Keep plan data deterministic and timestamp-free.
- [ ] Batch remote observations.
- [ ] Keep arbitrary repair backend behavior unchanged.
- [ ] Keep raw push guard behavior unchanged.
- [ ] Reject a non-SHA-1 object format before planning or mutation.
- [ ] Never update a managed code ref in a state-only action.
- [ ] Use exact state CAS.
- [ ] Stop the current attempt immediately after any post-CAS drift.
- [ ] Publish only the policy-declared owned derived integration ref; never a
      source/code ref.
- [ ] Re-run proofs during apply.
- [ ] Add an interruption test for every remote boundary.
- [ ] Preserve tombstones and unrelated state values.
- [ ] Never treat failed observation as absence.
- [ ] Never remove dirty or unique local artifacts.
- [ ] Keep primary checkout on integration.

### Before handoff

- [ ] Run focused new tests.
- [ ] Run existing coordination, guard, stack-land, and Syncwheel tests.
- [ ] Run the full test suite.
- [ ] Inspect the exact diff for private context and unrelated changes.
- [ ] Verify every changed CLI file has required version/changelog updates.
- [ ] Run `syncwheel validate`, `syncwheel plan --json`, and read-only handoff.
- [ ] Demonstrate plan determinism with two unchanged invocations.
- [ ] Demonstrate a no-op second maintain run.
- [ ] Demonstrate tree-different drift remains blocked.
- [ ] Report implementation readiness separately from live rollout authority.

## 23. Rollback and recovery

### 23.1 Source rollback

Reverting the feature release must not rewrite coordination history. Older
clients may see a maintenance-updated state as ordinary state and continue when
refs match. If they see an accepted-absent tombstone they do not understand,
they may fail closed; they must not recreate or delete data automatically.

Do not remove maintenance evidence from existing state commits.

### 23.2 Policy rollback

To stop future autonomous application:

```bash
syncwheel manifest maintenance manual
syncwheel manifest maintenance manual --apply --plan-digest <digest>
```

This is a forward control-plane change. Do not reset the manifest or state
branch to an earlier commit.

### 23.3 Partial state-only operation

If state CAS succeeded but local steps failed:

1. observe the remote state tip;
2. validate its parent and embedded plan digest;
3. rerun `maintain --plan-only`;
4. expect strict remote manifest adoption or local projection work;
5. do not retry the old state CAS.

### 23.4 Unknown derived-projection publication

Use existing coordinated publication observation. Do not rerun the push until
the remote outcome is known. `maintain` must surface the operation as unknown
and stop.

## 24. Security and trust assumptions

The local guard is a safety rail, not an authorization boundary. Another clone,
administrator, UI, API token, or `--no-verify` can move refs. This design remains
lossless under such movement by refusing unknown content and never overwriting
it during state-only maintenance.

The design assumes:

- Git SHA-1 object IDs and tree equality are trustworthy; version 1 rejects
  every other object format before planning;
- the remote returns an honest view to `ls-remote` and accepts exact state CAS;
- ordinary coordinated publication supports atomic push as already required;
- local filesystem atomic manifest writes retain their current durability
  guarantees;
- user/agent credentials have only the repository authority already required by
  normal Syncwheel publication.

A malicious remote can equivocate between observations. No client-only design
can prevent that. Post-verification and append-only evidence make the result
detectable; provider transparency or server enforcement is optional defense in
depth.

## 25. Acceptance criteria

The feature is ready for source delivery only when all of the following are
true:

1. The three synthetic failure fixtures converge autonomously under
   `safe-auto` when their required authority class is explicitly enabled.
2. The same fixtures remain plan-only under `manual`.
3. Every plan is deterministic and digest-bound.
4. Apply fails closed on state, ref, manifest, policy, ownership, or proof
   drift, and any post-CAS drift ends the current attempt before a derived
   update or cleanup.
5. State-only maintenance sends no application-ref mutation.
6. Every accepted state child is append-only with exact parent linkage.
7. Missing merged stack refs are closed, not recreated.
8. Tree-equivalent replacements update evidence without rewriting code.
9. Strict-behind local manifests adopt published declarations exactly.
10. Tree-different or unowned content always requires a semantic decision.
11. Owned derived integration publication retains atomic exact leases and can
    update only the policy-declared integration ref plus state.
12. Raw pushes remain blocked by the managed-ref guard.
13. An opted-in `main-integration` equals the declared ordered projection after
    success, with a recovery ref created before any local move.
14. Every product commit is owned by exactly one appropriate stack or is an
    explicitly recognized control commit.
15. Routine maintenance creates no persistent worktree.
16. Dirty, locked, unique, or external worktrees are preserved; local cleanup
    is absent by default and requires persistent `local_reap=safe-auto`.
17. Repeated maintenance is idempotent.
18. Existing manual repositories do not begin mutating autonomously after
    upgrade.
19. Core tests require no forge API or network service.
20. SHA-256 repositories fail closed with `unsupported-object-format`.
21. Full existing tests and version-bump guards pass.

## 26. Explicit boundary between readiness and live application

Completing the source code, tests, documentation, review, and release makes the
feature **ready**. It does not authorize:

- changing any existing repository's maintenance policy;
- repairing any live coordination state;
- installing hooks into live clones;
- applying fleet runtime changes;
- deleting existing worktrees, branches, backups, or stashes;
- pushing a source branch, opening a PR, merging, or releasing unless those
  delivery actions are separately authorized.

The first live repository migration must be a separately reviewed rollout with
its own plan digest, exact repository scope, pre/post evidence, and rollback
boundary. Once a repository has intentionally adopted `maintenance.mode=safe-auto`
with its explicit derived-projection and local-reap policy, future mechanically
proven state/evidence maintenance and declared derived integration maintenance
may run as part of an otherwise authorized repository task without a second
generic Syncwheel-repair approval. Source/code publication remains separately
authorized normal delivery work.
