# AI Agents

`syncwheel` is meant to reduce ambiguity for AI-driven Git maintenance.

## Contract

The script owns:
- repo state discovery
- manifest validation
- branch reconstruction commands

The AI agent owns:
- deciding when to update the manifest
- keeping authoring commits out of the shared primary checkout
- running project-specific validation after branch rebuilds
- communicating risks and blockers clearly
- deciding which exact stack revisions belong in a deployment channel and
  obtaining separate evidence for any external deployment

## Independent review and merge

For a selected managed delivery, inspect authority and private merge policy once
at intake, and again only after an upgrade, error or relevant state change:

```sh
syncwheel repo authority status --json
syncwheel repo pr-merge-policy status --json
```

`ai-managed` with `source_change` permits only its declared source scope.
`runtime_change`, a human-gated operation or an explicit repository requirement
retains its own authorization boundary. A merge preference never widens authority.
Explicitly authorized native maintenance follows [its separate procedure](native-maintenance.md)
without invoking the installed CLI or revision provider for this preflight.

Assign an independent reviewer subagent through the environment's authored role
routing. Record its identity, reviewed full SHA, verdict, findings and validation;
the writer cannot approve its own work. Resolve findings, request review of the
new delta and pin the resulting verdict to the final SHA. If the reviewer becomes
unavailable, explicitly reassign that bounded review and preserve the prior record.
Do not treat an old verdict as approval of a new head.

When the writer and reviewer share one GitHub account, record the independent
technical result truthfully with a `COMMENT`, identifying the reviewer and SHA.
It is not a distinct-account approval. Do not summon a human merely to obtain an
approval badge, fabricate another identity or self-approve. Explicit governance
requiring a human or distinct identity still applies until its authorized owner
changes that requirement. See [formal-review gates](github-pr-merge.md#review-evidence-and-formal-review-gates)
for the separately authorized, fixed-adapter exception.

Before merging, observe exact-head CI, outstanding conversations and live forge
rules. Retain plan/operation IDs and digests, merge/readback evidence and a
reconciliation receipt for uncertain outcomes. Source, merge, installation and
installed smoke remain separate claims.

## Lightweight delivery and acceptance

Use a light governed lane for ordinary authoring and commit work. Request a
`--full` lane only when the task names a concrete dependency, build, test or
debugging need that cannot be met by the light lane and the repository's CI.
Record that reason in the task or review record before opening the lane. Keep
the lane's work focused on the assigned change; a light lane is an operational
boundary, not a security sandbox.

Deliver through the target pull request and its required CI. Prefer CI on the
exact PR revision for build and test work. Where repository CI provides a
composition that can exercise the needed environment, use it instead of
provisioning a local environment. Retest in proportion to the changed behavior:
run focused tests for the affected behavior and the repository's required CI;
run broader suites when the change or its dependencies make them relevant.
Do not treat an optional composition or a local test as a substitute for
required CI.

Record acceptance as a concrete scenario, the person who approved it, the date,
and the exact revision or revisions covered. The agent assesses the candidate
delta since those revisions against the accepted scenario. Renew acceptance
only when the expected behavior changes or the delta's impact on that behavior
is uncertain. A passing test is evidence for its behavior, not a replacement
for acceptance of the intended behavior.

Keep replay dependencies and compatibility evidence separate. Use declared
stack dependencies only when one change requires another to be replayed first;
the dependency graph must be acyclic. A cycle is a dependency error to resolve,
not a compatibility-test result. Record compatibility combinations and their
test evidence separately; do not add ordering dependencies just to express that
two stacks should be tested together.

## Delivery boundaries and violations

In stack-managed repositories, every product commit included in the shared
integration projection or published as a managed stack must be declared in the
manifest and attributable to its owning stack or an approved historical
classification. Use the documented capture or reconciliation path; do not
leave unregistered product commits in that projection or publish them as
managed work.

Keep other delivery and metadata paths accountable through their own contracts:
journal repositories use the journal snapshot, pull and publish flow; Syncwheel
source maintenance follows the separate native-maintenance procedure; and
manifest or other control-plane metadata changes use an admin checkout or a
dedicated maintenance PR, excluded from `integration.stacks`. Record the owner,
reason, change and review evidence required by that path. Do not create stacks
or historical product classifications for journal entries, native maintenance
or control-plane metadata.

Use only project-authorized branches, builds, artifacts and folders for local
environment changes or deployment. An authorization for a source change or a
pull request does not by itself authorize an environment install, deployment or
service change. Do not switch to another clone, checkout, build output or folder
to evade branch, CI, review or deployment rules. Follow the repository's
deployment procedure and retain evidence that ties the deployed artifact to its
approved source revision.

Syncwheel can validate and manage the refs and metadata described by its
manifest. Its local CLI and hooks do not secure arbitrary clones, local build
tools, environment credentials or deployment systems; repository CI, forge
protections and the deployment system must enforce those boundaries. Treat a
confirmed violation as a blocker only for the affected delivery, until its work
is recovered and attribution is verified. Discovery or reporting of an issue is
not itself a violation. Do not impose a global block on unrelated work. When a
repeat violation is confirmed and attributed to an agent, require independent
review of that agent's next publication, regardless of lane. A successor who
takes over a lane does not inherit this requirement unless that successor is
independently found responsible. Record the evidence, affected delivery,
recovery and attribution; do not infer a violation from a warning or an
unresolved diagnostic alone.

Lane justification, scenario sign-off and acceptance renewal remain workflow
requirements. Repositories may explicitly enable the
[native attributed PR recovery gate](actor-recovery.md) for repeat-violation
review; its configured publication targets, independent authority and residual
surfaces are part of that opt-in contract. Otherwise repeat review remains a
workflow requirement. The CLI exposes
`worktree open --full` and records lane metadata; it does not determine whether
the task needed a full lane or whether a person approved its behavior. Use the
repository's PR, CI and deployment controls for those decisions.

## Ratified working rules (read this first)

Follow these four operating rules before changing a managed repository.

1. Never author or commit in the primary checkout; it stays on
   `manifest.integration.branch`. Open a governed lane: `syncwheel worktree open <lane>
   [--into <stack>] [--full]`. As of 0.42.4, where hooks are installed
   (`syncwheel hooks install --apply`), a manual commit or unauthorized integration-ref
   move in the primary is refused. Product-changing mutations refuse while it is dirty;
   bounded metadata operations preserve unrelated edits as described below. The only
   opt-out is a reasoned, ledgered disable: `syncwheel hooks remove --disable --reason
   "<why>" --apply`. Export `SYNCWHEEL_LANE_OWNER=<agent-name>` for the whole session:
   it owns the lanes you open and is recorded as `actor` on stack create, add, set,
   capture, promote, close-intent, and push ledger events, so a refusal blocked by your
   unpublished stack names you. Without it the actor is `<user>@<host>:<parent-pid>`.
   An expired lease never removes a lane, and neither does the death of the
   opening process: only `worktree release <lane>` or `gc --apply` removes one.
2. Never resolve a replay conflict with raw git. Take the retry the conflict names:
   `syncwheel stack rebuild <id> --replay-mode desk`, then resolve through the manifest with
   `syncwheel stack absorb <stack> [<path>...|--staged]` or `syncwheel stack
   resolve-integration <stack> <resolved-commit>...`. A manual `git merge`/`git commit` on
   integration is invisible to Syncwheel: the next rebuild reconstructs the branch from the
   manifest's own commit projection and never consults that resolution, so the work is lost
   and `reconcile` keeps refusing with the same conflict.
3. Before the first use of each mutating subcommand in a session, or after an
   upgrade/error, run its exact nested `--help`; never infer flags, close reasons,
   defaults, or force requirements from memory. Integration composition is
   declared and visible — inspect it with `syncwheel int show`
   before testing there or blaming your own code. Add a stack with `syncwheel stack create
   <id> [<commit-or-range>...] [--draft]` then `syncwheel int rebuild --reason "<why>"`;
   close a normally merged stack with `--reason merged`, or a squash/rebase delivery
   with `--reason absorbed`, then validate and rebuild. `absorbed` verifies final
   delivered content and does not require `--force`.
4. Record a reason for each mutation. Use `--reason` when the subcommand's
   `--help` lists it (some commands require it, including `int rebuild` in
   ai-managed repositories). Otherwise, record the reason and command in the
   work receipt, as for `stack add`. Inspect Syncwheel's events with
   `syncwheel ledger show`.

**After a refusal on managed state:** stop and report the command, error, and
next documented remedy. Do not bypass it with raw `git push`, a manual managed
manifest or coordination state edit, or an internal mutation helper. Re-observe
and follow the named remedy when its preconditions are met. For a managed ref
that disagrees with the coordination state, use `syncwheel coordination repair`
(plan-first, then `--apply
--plan-file <plan>`): default for a wrong recorded tip on an otherwise-correct ref (ref
repair), `--freeze-backend tree-equivalent-state-cas` for same-tree/different-shape,
`--freeze-backend fast-forward-state-cas` for an exact reviewed fast-forward,
`--freeze-backend missing-ref-create-cas` to atomically recreate an absent owned
ref at its recorded tip, and `--freeze-backend state-digest-migration` for a
pre-0.42.2 legacy-digest state.

## Recommended prompt flow

A human should be able to write:
- `syncwheel this repo`
- `rebuild integration and all PR branches`
- `validate stack drift and tell me what is missing`
- `reconcile this shared integration branch with the manifest`
- `show how channel test differs, plan it, and stop before applying`

An AI agent should then:
1. run `syncwheel reconcile`
2. if the manifest is missing or stale, update it first with `init` and
   `stack create`/`stack set`/`stack add`
3. run `reconcile --apply --worktree-root <path>` only when the dry-run plan is
   understood
4. add `--push` only when the shared remote branches
   should be updated
5. rerun `check` or `reconcile`
6. summarize what changed and what still needs a human

For an active-active version 2 or 3 manifest, insert `syncwheel
handoff` before planning or publication. It is a read-only diagnostic of the
published state, ownership boundary, local locks, pending merge decision, and
eligible cleanup. Use `publish` rather than a raw Git push so all managed refs
and the coordination state receive one atomic, leased publication.

The lease authority for a managed source is its
`refs/heads/syncwheel/claim/heads/...` claim. A valid publish advances the claim
in the same atomic refspec; merely observing or leasing an unchanged ref is not
proof. Treat `coordination.claims: advisory` as a migration state, inspect
`syncwheel coordination claims backfill`, and never flip to `required` until
the dry-run reports zero unclaimed owned refs. Backfill with `--apply --reason`
never replaces a claim owned by another coordination domain.

Targeted `stack push` changes only its selected stack branch and the corresponding
claim/state refs. Control-manifest persistence is local housekeeping, even when the
integration branch is also the canonical product branch. Publish or bootstrap
integration explicitly with `int push`; a stack push never grants that authority.
Its dry run lists the same source refs that execution may publish. A legacy pending
intent with extra refs is completed only from exact landing evidence; if it never
landed, it is abandoned and replaced with a stack-only intent.

Every coordinated remote mutation must have a fsynced caller intent and stable
operation token. On retry, exact token-bearing claim/state evidence is adopted;
do not repeat the push. Create propagates its `stack_create_intent` token into
the claim and removes only the deterministic temporary worktree owned by that
generation.

Take the clone's publication lock around the whole intent, push, local
remainder and terminal event. The lock, not the intent payload, says what is in
flight: a command that cannot take it exits 2 naming the live owner and touches
nothing, and one that takes it knows no other cycle is running here, because
process death releases the lock. The owner record in the intent names a dead
owner in the abandonment event; it never authorizes a transition.

Landing has two proofs, and every caller declares which it accepts. The state
proof is a state commit inside the intent's recorded window carrying its token,
scope, changed refs, projection status and manifest digest, plus a claim
carrying that token for every touched source ref; it is the only form a fresh
publication may use to decide it needs no push. The claim proof belongs to the
recovery paths alone and only after the state proof failed: every changed ref,
and no other, carries a claim declaring this operation's token, scope and whole
ref set. It has no window, which is why it survives a state ref rewritten
backwards, and it excludes the manifest digest, which no claim records; a claim
written before 0.43 records no scope or ref set and therefore proves nothing.
Only the most recent 500 claims of a source ref are read. Neither proof reads
current ref tips: in an active-active fleet another clone may legitimately move
a published ref, push the same tip, or reuse a caller-supplied identifier. When
no evidence holds, record `coordination_publish_abandoned` with reason
`not_landed` or `superseded` and continue rather than freezing on it, and undo
the local rename only when the remote carries neither the promoted ref nor a
claim bearing that token.

A reconcile retry with a non-empty newly planned refset adopts an existing intent
only when the whole changed-ref tuple matches. Otherwise the same authenticated
landing resolver must settle it before a fresh intent can be created. A landed
operation is never abandoned to make a different tuple publish; uncertain evidence
stops the command. Empty-action retries retain their existing recovery contract.

The manifest digest that identifies an operation is computed over the public
manifest snapshot without `integration.derived_provenance`: the intent and the
published state resolve that projection from different sources by design, so it
cannot be part of an identity. An intent recorded before 0.43.0 carries the
older digest, is therefore never proved landed by the state proof, and is
terminalized under the same rule as any other intent whose evidence is absent.

Write exactly one terminal record per operation token, refusing a second when it
is written rather than when the ledger is read. A landed promotion still owes
only its manifest save; `stack promote`, `stack push`, `int push`,
`stack create`, `stack close`, `stack rebuild`, `stack add`, and
`reconcile --apply` complete it from the published state and rebuild the
promoted branch instead of requiring a particular local branch layout, anchoring
a diverged draft branch under `refs/syncwheel/recovery/drafts/` before dropping
it. When a different clone completed the same promotion without leaving a local
intent, `stack promote` may adopt it only from exact published stack, ref, and
tombstone evidence; adoption must not publish a new remote state. Never leave a
publish intent that no command can terminalize, and never let
a foreign pending intent be the reason an unrelated publication cannot run.
`stack sync` only records the stack branch in the selected manifest. It preserves
unrelated unstaged primary product files, the index, ignore files and other refs.
It refuses pending promotion or control recovery with an explicit remedy instead
of performing recovery or worktree cleanup as a side effect.
An explicit `--manifest` overrides an inherited personal profile or environment
selection without changing that profile. Explicit `--manifest` and `--personal`
flags remain mutually exclusive.

For draft close, the order is intent, remote tombstone claim plus state CAS,
local manifest save, terminal event. Do not reintroduce remote observations
around the filesystem save: they cannot make that boundary atomic. A retry may
complete only when the remote tombstone contains its exact operation token.
If the claim has advanced, record `close_superseded` and stop without applying
the old close to the current generation; decide that from the claim of that
generation, never from a state tip that unrelated publications also move.
Remote failure during either the first attempt or recovery must name the same
remote-first retry command.

For channels, inspect `channel contract`, `channel list`, `channel show`, and
`channel diff` first. Every mutation previews a `channelPlan`; repeat the same
command only with its exact `--plan-digest ... --apply` and optional stable
`--operation-id`. Use `channel plan` for materialization/publication. Publish
with `channel publish`; never replace its exact lease with a raw Git push. Under
active-active coordination the channel ref and state publish atomically. A
published branch is only a deployment input, not proof that an environment was
deployed.

The channel base is an exact pin. Treat `baseDrifted` as information, not an
instruction to follow the symbolic base; only an explicit `channel refresh`
advances it. Preserve every stack's exact `depends_on` closure and order. A
channel-local resolution snapshot is bound to `forPinDigest`; composition edits
invalidate it and promotion copies it with the source pins. Shared channels
refuse draft-stack publication, while ephemeral channels may carry drafts. An
active-active channel must use the coordination remote.

## Targeted worktree recovery

`stack git --worktree` / `--auto-worktree` and `stack add` limit automatic
cleanup and ledger recovery to lanes targeting the selected stack. Recovery of
terminal records, pending intents and moved paths follows the same scope; unrelated
lanes and retained stale-lock evidence remain unchanged. Capturing a newly owned
active lane still requires its entire commit set to belong to that stack.
Use explicit `worktree release` or `gc --apply` for unrelated lifecycle recovery;
`gc --lane` confines recovery to the named candidates. Real locks, generation
checks, registry CAS and guarded `stack add --at-source` checks remain enforced.

## Safety rules

- do not author or commit in the shared primary checkout, even when it is clean;
  open a governed lane first with `syncwheel worktree open <lane> --into <stack>`
- when the calling checkout is behind the stack, use `worktree open <lane> --into
  <stack> --base <stack-branch>`; the explicit base must resolve to the stack's
  exact current projected tip, and the lane records that commit as its base
- product-changing mutations stop on unrelated dirty primary files. Personal authority
  updates, private PR policy set/clear, empty non-coordinated draft close with its default
  reason or `closed` and without force or branch deletion, non-coordinated archive
  and stack sync without pending recovery may proceed while preserving those
  files. Index conflicts, unrelated staged paths and a dirty shared control manifest
  still stop these operations. Pending control recovery requires its explicit remedy
- metadata saves do not normalize ignore files or force-stage authority changes;
  use explicit tracking configuration when exclusions need updating
- read-only diagnostics remain usable when the primary is dirty; treat their yellow
  dirty-primary warning as foreign work, not permission to repair it
- use `status` without `--fetch`, `check --no-fetch`, or `worktree list` to
  preserve control metadata and skip startup maintenance and authorization
  cleanup; delivery validation may still calculate temporary Git projections
- for uncommitted tracked primary changes, identify their owning stack and use
  `syncwheel stack absorb <stack> <paths> -N -m "message"`. Default amend refuses
  an empty stack; it must not amend the base commit
- use `syncwheel stack capture-integration <stack> HEAD` only for already committed
  primary work named by a refusal; do not create a new primary commit to use it
- capture validates the full proposed integration before persisting ownership.
  Pending control recovery is explicit: follow the scoped command printed for
  the selected manifest and retry capture after it completes
- capture updates the selected manifest and ledger without staging or committing
  control metadata; preserve tracked manifest changes for separate delivery
- use `syncwheel hooks remove --disable --reason "..." --apply` only as a deliberate,
  visible clone-local recovery opt-out; re-enable the bundle afterwards
- when a primary-checkout stop names a manifest-derived remedy, use the named
  `stack capture-integration <stack> HEAD` command for your committed primary
  work, or `worktree open <lane> --into <stack>` without changing work owned by
  another agent; capacity and expiry messages name the applicable `stack add`
  queue command
- treat a light lane as an operational boundary, not a sandbox: do not install
  dependencies there, and return to a clean checkout before stack ownership
  operations
- review governed-worktree warnings on normal commands, which never remove a
  lane; with `gc --apply` or `worktree release`, a missing lane with an
  expired lease or known dead local owner is recovered to a local recovery ref,
  and pushed to `refs/syncwheel/archive/lanes/...` on the publication remote,
  before reaping even if its registry path is outside the current root, while an
  existing dirty or unknown lane must remain visible until explicitly handled
- capturing commits does not authorize removing another owner's live lane.
  Keep its active state and lease, and coordinate an explicit release with its
  owner when the command reports `captured, kept`. Unproven lease expiry also
  retains a foreign lane
- preview `worktree release <lane> --reason <why>` before using `--apply` for a
  dead or abandoned lane; apply records the reason in the ledger and refuses an
  existing dirty worktree with its recovery remedy
- for `captured_pending_cleanup`, first leave the lane directory, then preview the
  named `worktree release <lane> --reason "complete captured lane cleanup"` with
  the same `--manifest` or `--personal` selector; add `--apply` only after review.
  This works without active-active GC and preserves a recovery ref plus a remote
  archive ref; if the archive push fails the lane stays (`archive_push_failed`).
  Only commits are saved: commit uncommitted work first. Dirty bytes,
  live users and foreign locks still stop cleanup; never release another owner's
  lane merely because its warning is visible
- use `--dry-run` when inspecting rebuild/push commands
- prefer `reconcile` for the normal multi-device lifecycle. Raw Git is for
  inspection; there is no raw-push fallback. Stop and report a Syncwheel refusal
  instead of bypassing it
- for an active-active manifest, use `handoff` before taking over from another
  device or agent; never bypass the coordinated publisher with `git push`
- when a publication or revision-provider check is blocked by another stack's
  unpublished declaration, contact the owner it names and run `syncwheel
  handoff`; the only command these refusals suggest is `syncwheel stack set <id>
  --published`, offered for a stale local commit change whose published commits
  are present locally (it prints the command that restores them)
- if a coordinated publish reports a mergeable race, do not retry silently;
  review the handoff and use `publish --accept-merge` only for that explicit
  disjoint-stack decision
- keep local worktrees that need investigation with `worktree lock <stack>`;
  `gc --apply` reaps eligible expired governed lanes whether or not active-active
  coordination is enabled, removes only eligible local tombstoned artifacts, and
  never deletes a remote branch; `gc --apply --lane <lane>` removes only the named
  lanes. Other mutating commands never remove a lane because its lease expired
- if manifest and Git disagree, fix the manifest or call out the conflict explicitly
- do not claim a repo is aligned if integration and PR branches still disagree
- stop on a stale channel plan, replay conflict, unknown required observation,
  post-plan remote change, or lease loss; re-observe instead of forcing the ref
- after an uncertain ref or remote outcome, inspect `channel operation show`
  and use digest-bound `channel operation reconcile`; it only observes and
  appends a terminal receipt, never retries the mutation
- treat `cancelled` before the authoritative boundary as terminal; an interrupt
  at or after that boundary is `unknown` until reconciliation
- close expired or obsolete channels explicitly; expiry is not automatic
  branch deletion
- fail closed when a source claim is absent in required mode, belongs to another
  domain, or fails to advance; use handoff or claims backfill, never a raw push

## Manifest tracking

Before branch, push, PR, or recovery work, inspect the repo-local tracking policy:

```bash
syncwheel repo tracking status
```

`syncwheel_tracking=git-tracked` means `.syncwheel/manifest.json` is part of the
shared Git contract. `syncwheel_tracking=local-only` means Syncwheel metadata
must stay local through `.git/info/exclude`: without active-active coordination
no control commit is created on the integration branch, and no Syncwheel push
carries the manifest to a stack, pull-request or delivery branch. If the policy is missing,
ask for the intended mode and persist it with
`syncwheel repo tracking set ... --apply` before continuing. See [`manifest-tracking.md`](manifest-tracking.md) for the
full policy and migration behavior. An installable agent skill that encodes this
lives in [`skills/syncwheel/SKILL.md`](../skills/syncwheel/SKILL.md).

Selecting a personal manifest limits `repo tracking set` to that manifest and its
private ledger. It does not migrate shared ignore files, hooks or the Git index.
The selected private metadata must already be ignored and untracked.
The command prepares the manifest and ledger update before publication and restores
its own writes if publication raises a handled error. This rollback does not
provide automatic recovery from process termination or power loss.

## Repository authority

The manifest can also say how much of the delivery pipeline you may run without
a human gate:

```bash
syncwheel repo authority status
```

- `human-gated` (or no block in the primary shared manifest): ask before commit, push, PR, and merge, as
  the repository's own instructions require.
- Neither mode reserves refreshing the local shared integration branch: it is staging, not storage. See "The shared integration branch is staging, not storage" in the Syncwheel skill.
- `ai-managed`: the repository's configured Syncwheel pipeline runs unattended.
  The manifest defines the pipeline (stacks, PRs, landing, channels, or journal
  publish); `authority` only says whether you may run it without a human at
  each stage. Still stop for anything outside the named scope, for external
  side effects, and for anything destructive.
- `source_change` covers source delivery up to and including merge or
  `journal publish`. `runtime_change` covers the rollout that pipeline leads to
  (deployment channels, install, service restarts) and must be listed on its
  own; otherwise rollout remains gated.
- `destructive_rewrite` is never allowed, whatever the block says: force
  pushes, history rewrites, and deleting work always need a human.

A lane or personal manifest without a declaration inherits the primary checkout's
shared manifest policy. An explicit declaration can retain or narrow its grants;
it cannot add capabilities denied or not granted by the primary policy. Inspect
the effective policy and its source with `repo authority status`. Inherited
authority is not written into the lane manifest.

Only a maintainer sets this policy (`syncwheel repo authority set ... --apply`).
Never set it, widen it, or infer it for yourself.

## GitHub PR merge policy

An `ai-managed` delivery repository may opt into the deterministic GitHub PR
merge path with a private clone-local policy. Configure it with
`repo pr-merge-policy set github`, inspect the dry-run, and apply only after
reviewing the exact local diff. Then use `stack merge-pr <stack>` to produce a
digest-bound JSON plan. See [`github-pr-merge.md`](github-pr-merge.md) for the
full contract.

The policy is fail-closed by default: it requires an allowlisted actor with
repository admin permission, an allowlisted PR/commit/source repository, an
exact stack head, green CI, no unresolved review work, and recognized GitHub
rules. Repositories without CI may explicitly set clone-local `checks: "none"`;
the plan reports that opt-out as a warning and still honors required GitHub
checks. The
admin bypass is limited to required reviews and always includes
`--match-head-commit`; no branch deletion is performed.

## Agentwheel installable skill

When Agentwheel is available, install the Syncwheel agent skill into the local
Codex runtime for the repo:

```bash
agentwheel install github:NestDevLab/syncwheel --adapter codex --local --target-root /path/to/repo --skill syncwheel
```

`syncwheel self status` reports a missing-skill recommendation when Agentwheel is
installed and the skill is absent. If the `syncwheel` executable is not on PATH
yet, use the checkout script directly:

```bash
python3 /path/to/syncwheel/scripts/syncwheel.py self status
```

## Source architecture

See [Source module boundaries](module-architecture.md) before moving command or
domain logic. Preserve the CLI facade, explicit I/O patch points and package
module declarations when extending a domain.
