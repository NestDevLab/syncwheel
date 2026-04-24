# Syncwheel branch model

## Intent

Keep four concerns separate:
- canonical upstream history
- publication remote history
- integration/runtime history
- PR review surfaces

## Default model

Unless a repo documents otherwise:
- `main` or equivalent is the canonical base
- `integration/*` is where day-to-day work happens
- `pr/*` branches are extracted review surfaces for upstream PRs
- integration should not be the only home of long-lived product changes

## Deterministic mapping

The important step is not just naming branches. It is declaring:
- which commits belong to which logical stack
- which stack maps to which `pr/*` branch
- in what order stacks are replayed into integration

Without that mapping, Git can only infer ownership heuristically.

With that mapping, syncwheel becomes scriptable.

## Worktrees

Recommended layout:
- repo root = administrative checkout
- one worktree for the active integration branch
- one worktree per active PR branch
- temporary worktrees for rebuilds only when needed

## Safe defaults

- base PR branches from canonical main
- do normal development on integration
- make every persistent integration change also belong to a PR stack
- keep integration-specific glue visible and rare
- prefer declarative stack repair over ad-hoc rebases

## Visibility rule

If IDE UI and `git status` disagree with reality, trust the full graph plus the syncwheel manifest, not the branch badge.
