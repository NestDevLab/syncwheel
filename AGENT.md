# Syncwheel For AI Agents

Use Syncwheel for deterministic Git maintenance in repositories with PR stacks, integration branches,
dedicated worktrees, forks, or more than one human/agent touching branches.

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

For a version 2 manifest with `coordination.mode: "active-active"`, run this
read-only handoff check before planning a mutation or taking over from another
device:

```bash
syncwheel handoff
```

## Mutation Rules

- `reconcile` is read-only by default.
- `sync`, `publish`, `reconcile --apply`, stack rebuilds, integration rebuilds, branch deletion, and
  pushes are mutations.
- Never mutate branches from a dirty worktree.
- Prefer dedicated worktrees under the declared Syncwheel worktree root.
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
- Core procedure: `docs/core-procedure.md`
