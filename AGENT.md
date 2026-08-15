# Syncwheel For AI Agents

Use Syncwheel for deterministic Git maintenance in repositories with PR stacks,
integration branches, pinned deployment channels, dedicated worktrees, forks,
or more than one human/agent touching branches.

## First Checks

Run these before branch, worktree, push, PR, recovery, or final handoff work:

```bash
syncwheel repo tracking status
syncwheel status --fetch
syncwheel validate
syncwheel reconcile
```

If `syncwheel_tracking` is missing, stop and ask whether the repo should be `git-tracked` or
`local-only`. Persist the answer with `syncwheel repo tracking set ... --apply` before continuing.

For a version 2 or 3 manifest with `coordination.mode: "active-active"`, run this
read-only handoff check before planning a mutation or taking over from another
device:

```bash
syncwheel handoff
```

## Mutation Rules

For `repository_mode: "journal"`, do not use stack or integration commands.
Run `journal status`, then plan with `journal snapshot` or `journal publish`;
add `--apply` only for an authorized commit or exact-lease push. Scheduler
install/remove is also plan-first and Linux-only.

- `reconcile` is read-only by default.
- `sync`, `publish`, `reconcile --apply`, stack rebuilds, integration rebuilds, branch deletion, and
  pushes are mutations.
- `channel list`, `channel show`, `channel diff`, and `channel plan` are
  inspection/planning operations. Composition edits, `channel apply`, `channel
  publish`, promotion, and close are mutations.
- Treat channel apply as plan-bound: never reuse a stale plan or bypass its
  observation revision and digest. Treat channel publish as an exact-lease
  operation; a published channel branch is not evidence of an environment
  deployment.
- After an uncertain local-ref or remote outcome, inspect the actual ref and
  latest channel ledger receipt before creating a new plan. Never retry blindly.
- A channel pins its base as well as its stack revisions. Only `channel refresh`
  advances the base pin. Shared channels refuse draft-stack publication;
  ephemeral channels may use drafts, and active-active channels must use the
  coordination remote.
- Never mutate branches from a dirty checkout.
- Rebuilds do not create a worktree. Ask for one with `--replay-mode desk` only when you need to
  resolve a conflict or build/test a branch in isolation, and put it under the declared Syncwheel
  worktree root.
- A plumbing conflict stops and prints the `--replay-mode desk` retry command. Take it: `stack absorb`
  and `stack resolve-integration` need a checkout that plumbing never created.
- For active-active repositories, never bypass `publish`, `stack push`, or
  `int push` with a raw `git push`; they provide the required atomic state and
  exact leases.
- If publication reports a mergeable race, inspect `handoff` and use
  `publish --accept-merge` only after explicitly accepting the disjoint-stack
  merge. Do not retry a failed lease silently.
- Use `worktree lock <stack>` before retaining a tombstoned worktree for local
  investigation. `gc --apply` never deletes remote branches.
- After rebuilds, diff the result against the expected post-fix state so stale manifest projections
  do not silently revert work.

## Install The Skill

When Agentwheel is available:

```bash
agentwheel doctor --adapter codex --local --skill syncwheel --source github:NestDevLab/syncwheel
agentwheel install github:NestDevLab/syncwheel --adapter codex --local --skill syncwheel
```

## Handoff Shape

End with:

- manifest tracking status
- worktree cleanliness
- validation/check results
- commit/push state for git-tracked repos
- active-active handoff state, pending merge decision, and cleanup candidates
- any branch or remote action still needing a human decision

## Key References

- Install handoff: `install.md`
- AI agents: `docs/ai-agents.md`
- Agent procedure: `docs/agent-procedure.md`
- Manifest tracking: `docs/manifest-tracking.md`
- Active-active protocol: `docs/design/active-active-coordination.md`
- Deployment channels: `docs/deployment-channels.md`
- Core procedure: `docs/core-procedure.md`
