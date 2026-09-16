# Workflow

## Intended branch model

`syncwheel` keeps five separate concerns:
- canonical upstream history
- publication remote history
- integration/runtime history
- PR review surfaces
- pinned channel compositions used as deployment inputs

Default operating stance:
- day-to-day work happens on `main-integration`
- every persistent integration change should map to one PR stack
- each stack maps to one `pr/*` branch
- integration is rebuilt as an ordered replay of declared stacks
- channels are optional selected compositions whose exact stack revisions move
  only through explicit channel operations

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
   `reconcile --apply --push --worktree-root <path>` when
   the shared remote state should move
5. rerun validation or `reconcile`
6. run project-specific tests outside `syncwheel`

## Replay execution

A rebuild leaves no worktree behind:

```bash
syncwheel stack rebuild feature-a
syncwheel int rebuild --reason "refresh integration projection"
```

The default mode, `auto`, replays through Git plumbing when Git supports
`merge-tree --write-tree` (2.38 or newer) and in a detached temporary worktree
below that threshold. It uses the current checkout when the target branch is
already the current one, and an existing desk when the branch already has one.
Capability is detected at runtime and an inapplicable mode falls back rather
than failing.

Desk is an escalation/validation surface, not routine authoring: begin routine
implementation, dependency installation, builds, and tests on integration. Ask
for a desk only to resolve a conflict or validate a non-empty materialized stack
when integration cannot safely run it:

```bash
syncwheel stack rebuild feature-a --replay-mode desk --worktree <path>
syncwheel stack rebuild feature-a --in-place
```

A plumbing conflict prints the conflicted paths and a literal retry with
`--replay-mode desk`; it does not silently create a checkout where the conflict
could be resolved. Plumbing also requires its target branch not to be checked
out, so `auto` uses in-place or desk for an active integration checkout.

Pin a default when `auto` is not what you want, most specific first: the
`--replay-mode` flag on the command, then the repo-local profile, then the
manifest.

```bash
syncwheel replay-mode              # show the effective mode and where it came from
syncwheel replay-mode ephemeral    # set the repo-local default
syncwheel replay-mode --clear      # fall back to the manifest, then to auto
```

The repo-local value lives in `.syncwheel/profile.local.json`, which is never
committed. `defaults.replay_mode` in the manifest is the shared default for a
repository whose contributors should agree on one; it is deliberately excluded
from coordination state, because it changes how a ref was produced, not what it
contains. Each rebuild records the mode it used in its ledger event, and
`plan --json` names the mode a rebuild would take.

## Deployment-channel workflow

Use a channel when CI/CD or a human needs a stable branch containing a selected
set of stacks rather than the full moving integration projection:

1. create a shared or ephemeral channel;
2. add, remove, replace, or refresh its pinned stack entries; refresh also
   advances the exact pinned base revision;
3. preserve dependency closure/order and use a channel-local resolution
   snapshot only for conflicts;
4. inspect `channel diff`; preview every mutation and apply only its exact
   `planDigest`;
5. validate the resulting branch, then publish it with an exact lease;
6. obtain separate evidence from CI/CD before reporting an environment as
   deployed;
7. close the channel explicitly when it is no longer an external deployment
   input.

The plan is invalid after any relevant manifest, stack, base, local branch, or
remote observation changes. Re-plan instead of retrying a stale apply or
publication. If an outcome is uncertain, `channel operation reconcile` only
observes current state and appends a digest-bound terminal receipt; it never
retries the mutation. See [deployment-channels.md](deployment-channels.md) for
commands and failure behavior.

If `plan` identifies an integration commit with no owner and the PR shape is
still undecided, create a draft and capture it instead of leaving it on
integration:

```bash
python3 scripts/syncwheel.py stack create exploration --draft \
  --purpose "Classify integration-first work"
python3 scripts/syncwheel.py stack capture-integration exploration <commit>...
```

Capture rebuilds only the draft source branch. It does not mutate integration;
the next `int rebuild` reprojects integration from the now-owned commit.

## Manifest semantics

- `defaults.base_ref`: canonical ref used as replay base
- `integration.branch`: the real integration branch
- `integration.stacks`: replay order of logical stacks into integration
- `stacks[].branch`: PR branch for that stack
- `stacks[].commits`: exact commit list for that logical stack
- `channels[].composition`: exact stack pins in channel replay order
- `channels[].baseRevision`: exact base commit, changed only by explicit refresh
- `channels[].resolution`: optional exact snapshot bound to the current raw pins

For a captured integration-first commit, the manifest retains the integration
SHA. Deterministic replay on an unchanged base reproduces that SHA on the stack
branch, so both branch and integration containment checks converge.

`validate` also reports non-merge commits that exist on integration after
`integration.base` but are not declared in any stack. These commits need a
manifest update or an explicit decision to keep them temporary.

## What remains non-deterministic

These still need human judgment:
- whether a commit should be split across two stacks
- whether a reconciliation commit should stay integration-only temporarily
- whether two rewritten commits are conceptually the same fix

When that happens, update the manifest deliberately instead of relying on branch names or memory.
