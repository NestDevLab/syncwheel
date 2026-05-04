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

## Recommended prompt flow

A human should be able to write:
- `syncwheel this repo`
- `rebuild integration and all PR branches`
- `validate stack drift and tell me what is missing`
- `reconcile this shared integration branch with the manifest`

An AI agent should then:
1. run `python3 scripts/syncwheel.py reconcile`
2. if the manifest is missing or stale, update it first with `init` and
   `stack create`/`stack set`/`stack add`
3. run `reconcile --apply --worktree-root <path>` only when the dry-run plan is
   understood
4. add `--push -- --force-with-lease` only when the shared remote branches
   should be updated
5. rerun `check` or `reconcile`
6. summarize what changed and what still needs a human

## Safety rules

- do not mutate branches from a dirty worktree
- prefer dedicated worktrees for every rebuild step
- use `--dry-run` when inspecting rebuild/push commands
- prefer `reconcile` for the normal multi-device lifecycle; use raw Git only as
  inspection or fallback
- if manifest and Git disagree, fix the manifest or call out the conflict explicitly
- do not claim a repo is aligned if integration and PR branches still disagree
