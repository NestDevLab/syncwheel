# Workflow

## Intended branch model

`syncwheel` assumes a repository with four separate concerns:
- canonical upstream history
- publication remote history
- integration/runtime history
- PR review surfaces

Default operating stance:
- day-to-day work happens on `main-integration`
- every persistent integration change should map to one PR stack
- each stack maps to one `pr/*` branch
- integration is rebuilt as an ordered replay of declared stacks

## Why the manifest matters

Git alone can answer:
- which commits are on a branch
- whether branch A contains commit X
- how branch A differs from branch B

Git alone cannot answer with certainty:
- which commits belong to logical stack `foo`

That becomes deterministic only when the repository declares the mapping in `.syncwheel/manifest.json`.

## Basic procedure

1. recover and classify current state with `reconcile`
2. update stale stack commit lists with `stack sync`, `stack set`, or `stack add`
   when the report identifies real ownership changes
3. repair managed branch drift with `reconcile --apply --worktree-root <path>`
4. publish rebuilt managed branches with
   `reconcile --apply --push --force-with-lease --worktree-root <path>` when
   the shared remote state should move
5. rerun validation or `reconcile`
6. run project-specific tests outside `syncwheel`

## Manifest semantics

- `defaults.base_ref`: canonical ref used as replay base
- `integration.branch`: the real integration branch
- `integration.stacks`: replay order of logical stacks into integration
- `stacks[].branch`: PR branch for that stack
- `stacks[].commits`: exact commit list for that logical stack

`validate` also reports non-merge commits that exist on integration after
`integration.base` but are not declared in any stack. These commits need a
manifest update or an explicit decision to keep them temporary.

## What remains non-deterministic

These still need human judgment:
- whether a commit should be split across two stacks
- whether a reconciliation commit should stay integration-only temporarily
- whether two rewritten commits are conceptually the same fix

When that happens, update the manifest deliberately instead of relying on branch names or memory.
