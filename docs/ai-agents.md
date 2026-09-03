# AI Agents

`syncwheel` is meant to reduce ambiguity for AI-driven Git maintenance.

## Contract

The script owns:
- repo state discovery
- manifest validation
- branch reconstruction commands

The AI agent owns:
- deciding when to update the manifest
- deciding whether a temporary integration-only commit is acceptable
- running project-specific validation after branch rebuilds
- communicating risks and blockers clearly
- deciding which exact stack revisions belong in a deployment channel and
  obtaining separate evidence for any external deployment

## Recommended prompt flow

A human should be able to write:
- `syncwheel this repo`
- `rebuild integration and all PR branches`
- `validate stack drift and tell me what is missing`
- `reconcile this shared integration branch with the manifest`
- `show how channel test differs, plan it, and stop before applying`

An AI agent should then:
1. run `python3 scripts/syncwheel.py reconcile`
2. if the manifest is missing or stale, update it first with `init` and
   `stack create`/`stack set`/`stack add`
3. run `reconcile --apply --worktree-root <path>` only when the dry-run plan is
   understood
4. add `--push` only when the shared remote branches
   should be updated
5. rerun `check` or `reconcile`
6. summarize what changed and what still needs a human

For an active-active version 2 or 3 manifest, insert `python3 scripts/syncwheel.py
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

Every coordinated remote mutation must have a fsynced caller intent and stable
operation token. On retry, exact token-bearing claim/state evidence is adopted;
do not repeat the push. Create propagates its `stack_create_intent` token into
the claim and removes only the deterministic temporary worktree owned by that
generation.

When that evidence does not exist and the reviewed state tip has been overtaken,
the intent is dead: record `coordination_publish_abandoned` and continue rather
than freezing on it. Never leave a publish intent that no command can
terminalize, and never let a foreign pending intent be the reason an unrelated
publication cannot run.

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

## Safety rules

- do not mutate branches from a dirty worktree
- begin routine implementation, dependency installation, builds, and tests on integration;
  request `--replay-mode desk` only for conflict resolution or validation of a
  non-empty materialized stack when integration cannot safely run it
- when the primary checkout is explicitly owned, use `worktree open <lane>` as
  the only authoring fallback; choose `--full` only when that lane genuinely
  needs dependencies, builds, tests, or debugging
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
- fail closed when a source claim is absent in required mode, belongs to another
  domain, or fails to advance; use handoff or claims backfill, never a raw push

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
