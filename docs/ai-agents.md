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

An AI agent should then:
1. run `python3 scripts/syncwheel.py status --fetch`
2. run `python3 scripts/syncwheel.py validate`
3. run `python3 scripts/syncwheel.py plan --json`
4. if the manifest is stale, update it first
5. run `materialize-pr` or `materialize-integration` only when needed
6. rerun `validate`
7. summarize what changed and what still needs a human

## Safety rules

- do not mutate branches from a dirty worktree
- prefer dedicated worktrees for every materialization step
- treat `--apply` as branch mutation, not inspection
- if manifest and Git disagree, fix the manifest or call out the conflict explicitly
- do not claim a repo is aligned if integration and PR branches still disagree
