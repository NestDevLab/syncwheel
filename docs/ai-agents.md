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

## Ratified working rules (read this first)

Ratified operating rules (MGT-0206). Rule 2 is the one a real incident violated by
resolving a rebuild conflict with raw git instead of the documented path — the mechanism
existed, it just was not prominent enough. Follow all four exactly.

1. Never author or commit in the primary checkout; it stays on
   `manifest.integration.branch`. Open a governed lane: `syncwheel worktree open <lane>
   [--into <stack>] [--full]`. As of 0.42.4, where hooks are installed
   (`syncwheel hooks install --apply`), a manual commit or unauthorized integration-ref
   move in the primary is refused, and mutating commands refuse while it is dirty. The only
   opt-out is a reasoned, ledgered disable: `syncwheel hooks remove --disable --reason
   "<why>" --apply`.
2. Never resolve a replay conflict with raw git. Take the retry the conflict names:
   `syncwheel stack rebuild <id> --replay-mode desk`, then resolve through the manifest with
   `syncwheel stack absorb <stack> [<path>...|--staged]` or `syncwheel stack
   resolve-integration <stack> <resolved-commit>...`. A manual `git merge`/`git commit` on
   integration is invisible to Syncwheel: the next rebuild reconstructs the branch from the
   manifest's own commit projection and never consults that resolution, so the work is lost
   and `reconcile` keeps refusing with the same conflict.
3. Integration composition is declared and visible — inspect it with `syncwheel int show`
   before testing there or blaming your own code. Add a stack with `syncwheel stack create
   <id> [<commit-or-range>...] [--draft]` then `syncwheel int rebuild --reason "<why>"`;
   remove one with `syncwheel stack close <id> --reason "<why>"` then the same rebuild.
4. Every mutating command carries `--reason`; it is mandatory in `ai-managed` repositories
   and already enforced on several commands individually (`int rebuild` when ai-managed,
   `hooks remove --disable`, `worktree release`, `coordination provenance reset`). Pass it
   always. It lands in the ledger with the actor and the exact command:
   `syncwheel ledger show`.

**When something looks wrong:** do not force a push, do not hand-edit the manifest or the
coordination state, do not fall back to raw git. Re-observe and use the exact remedy the
failing command names. For a managed ref that disagrees with the coordination state, the
named repair classes are all `syncwheel coordination repair` (plan-first, then `--apply
--plan-file <plan>`): default for a wrong recorded tip on an otherwise-correct ref (ref
repair), `--freeze-backend tree-equivalent-state-cas` for same-tree/different-shape,
`--freeze-backend fast-forward-state-cas` for an exact reviewed fast-forward, and
`--freeze-backend state-digest-migration` for a pre-0.42.2 legacy-digest state.

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

## Safety rules

- do not author or commit in the shared primary checkout, even when it is clean;
  open a governed lane first with `syncwheel worktree open <lane> --into <stack>`
- do not run a built-in mutation while the primary is dirty: it stops before side
  effects and names `worktree open` or `stack capture-integration` for the work
- read-only diagnostics remain usable when the primary is dirty; treat their yellow
  dirty-primary warning as foreign work, not permission to repair it
- use `syncwheel stack capture-integration <stack> HEAD` only for already committed
  primary work named by a refusal; do not create a new primary commit to use it
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
- review governed-worktree warnings on normal commands; a missing lane with an
  expired lease or known dead local owner is recovered to a local recovery ref
  before reaping even if its registry path is outside the current root, while an
  existing dirty or unknown lane must remain visible until explicitly handled
- preview `worktree release <lane> --reason <why>` before using `--apply` for a
  dead or abandoned lane; apply records the reason in the ledger and refuses an
  existing dirty worktree with its recovery remedy
- use `--dry-run` when inspecting rebuild/push commands
- prefer `reconcile` for the normal multi-device lifecycle; use raw Git only as
  inspection or fallback
- for an active-active manifest, use `handoff` before taking over from another
  device or agent; never bypass the coordinated publisher with `git push`
- if a coordinated publish reports a mergeable race, do not retry silently;
  review the handoff and use `publish --accept-merge` only for that explicit
  disjoint-stack decision
- keep local worktrees that need investigation with `worktree lock <stack>`;
  `gc --apply` reaps eligible expired governed lanes whether or not active-active
  coordination is enabled, removes only eligible local tombstoned artifacts, and
  never deletes a remote branch
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

## Manifest tracking

Before branch, push, PR, or recovery work, inspect the repo-local tracking policy:

```bash
syncwheel repo tracking status
```

`syncwheel_tracking=git-tracked` means `.syncwheel/manifest.json` is part of the
shared Git contract. `syncwheel_tracking=local-only` means Syncwheel metadata
must stay local through `.git/info/exclude`. If the policy is missing, ask for
the intended mode and persist it with `syncwheel repo tracking set ... --apply`
before continuing. See [`manifest-tracking.md`](manifest-tracking.md) for the
full policy and migration behavior. An installable agent skill that encodes this
lives in [`skills/syncwheel/SKILL.md`](../skills/syncwheel/SKILL.md).

## Repository authority

The manifest can also say how much of the delivery pipeline you may run without
a human gate:

```bash
syncwheel repo authority status
```

- `human-gated` (or no block at all): ask before commit, push, PR, and merge, as
  the repository's own instructions require.
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

Only a maintainer sets this policy (`syncwheel repo authority set ... --apply`).
Never set it, widen it, or infer it for yourself.

## GitHub PR merge policy

An `ai-managed` delivery repository may opt into the deterministic GitHub PR
merge path with a private clone-local policy. Configure it with
`repo pr-merge-policy set github`, inspect the dry-run, and apply only after
reviewing the exact local diff. Then use `stack merge-pr <stack>` to produce a
digest-bound JSON plan. See [`github-pr-merge.md`](github-pr-merge.md) for the
full contract.

The policy is fail-closed: it requires an allowlisted actor with repository
admin permission, an allowlisted PR/commit/source repository, an exact stack
head, green CI, no unresolved review work, and recognized GitHub rules. The
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
