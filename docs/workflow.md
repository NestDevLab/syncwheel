# Workflow

## Intended branch model

`syncwheel` assumes a repository with four separate concerns:
- canonical upstream history
- publication remote history
- integration/runtime history
- PR review surfaces

Default operating stance:
- day-to-day work happens on `integration/*`
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

1. recover current state with `status`
2. validate the manifest with `validate`
3. inspect planned actions with `plan --json`
4. materialize individual PR branches if needed
5. materialize integration if needed
6. rerun validation
7. run project-specific tests outside `syncwheel`

## Manifest semantics

- `defaults.base_ref`: canonical ref used as replay base
- `integration.branch`: the real integration branch
- `integration.stacks`: replay order of logical stacks into integration
- `stacks[].branch`: PR branch for that stack
- `stacks[].commits`: exact commit list for that logical stack

## What remains non-deterministic

These still need human judgment:
- whether a commit should be split across two stacks
- whether a reconciliation commit should stay integration-only temporarily
- whether two rewritten commits are conceptually the same fix

When that happens, update the manifest deliberately instead of relying on branch names or memory.
