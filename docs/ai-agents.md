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
- review governed-worktree warnings on normal commands; clean expired lanes are
  recovered to a local recovery ref before reaping, while dirty or unknown lanes
  must remain visible until explicitly handled
- use `--dry-run` when inspecting rebuild/push commands
- prefer `reconcile` for the normal multi-device lifecycle; use raw Git only as
  inspection or fallback
- for an active-active manifest, use `handoff` before taking over from another
  device or agent; never bypass the coordinated publisher with `git push`
- if a coordinated publish reports a mergeable race, do not retry silently;
  review the handoff and use `publish --accept-merge` only for that explicit
  disjoint-stack decision
- keep local worktrees that need investigation with `worktree lock <stack>`;
  `gc --apply` removes only eligible local, tombstoned, remotely recoverable
  artifacts and never deletes a remote branch
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
