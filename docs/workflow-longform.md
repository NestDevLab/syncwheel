# Syncwheel

Syncwheel is a deterministic workflow for repositories that keep:
- a canonical upstream
- a publication fork or secondary remote
- one or more `pr/*` review branches
- one `integration/*` branch used for daily development or runtime validation

## Core idea

Work normally on integration.
Do not let integration become a black box.
Every persistent integration change should also belong to a named PR stack.

## Deterministic rule

To make the workflow scriptable, declare the stack model explicitly in:
- `.syncwheel/manifest.json`

That file should describe:
- remotes and canonical base
- integration branch and replay order
- PR stack ids and branches
- exact commits for each stack

## Primary CLI

```bash
python3 scripts/syncwheel.py status --fetch
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
python3 scripts/syncwheel.py materialize-pr <stack> --worktree <path>
python3 scripts/syncwheel.py materialize-integration --worktree <path>
```

## What becomes deterministic

With the manifest in place, the script can tell you:
- which commits belong to each stack
- whether the stack branch contains them
- whether integration contains them
- which branches need to be rebuilt
- in what order integration should be replayed

## What the AI should do

The AI should not guess the stack model from memory when the repo is meant to be maintained by syncwheel.

Instead, it should:
1. run the script
2. validate the manifest
3. rebuild branches from the manifest
4. validate again
5. report honestly

## Why this matters

Without a manifest, syncwheel is still possible, but partly heuristic.
With a manifest, syncwheel becomes repeatable and much easier to automate safely.
