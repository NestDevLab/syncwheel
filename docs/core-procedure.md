# Syncwheel core procedure

## Goal

Bring a fork/upstream/integration repo back to a state that is simultaneously:
- truthful
- reviewable
- operational
- scriptable

## Phase 1. Recover the actual repo model

Always inspect:
- remotes and what they really mean
- canonical base branch
- integration branch
- active `pr/*` branches
- worktrees
- stashes
- whether the repo root is an admin checkout or an active worktree

Use:
```bash
python3 scripts/syncwheel.py status --fetch
```

Questions that must be answered before edits:
- which remote is the canonical upstream?
- which remote is used to publish branches?
- which branch is the real integration branch?
- is there already a deterministic manifest?
- are there commits on integration that are not mapped to any PR stack?

## Phase 2. Recover or create the deterministic manifest

Preferred file:
- `.syncwheel/manifest.json`

If missing, create a starter file:
```bash
python3 scripts/syncwheel.py init
```

Then fill in:
- remotes and canonical base
- integration branch
- stack order inside integration
- one stack id per logical PR
- exact commits for each stack, preferably using `stack sync`, `stack set`, or
  `stack add`

Do not call the workflow deterministic until this file exists and matches reality.

## Phase 3. Validate the manifest against Git

Run:
```bash
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
```

Look for:
- missing commits
- branches missing locally
- commits declared for a PR branch but not contained there
- commits declared for a stack but not present on integration
- integration referring to unknown stacks

## Phase 4. Repair PR branches deterministically

For each stack that needs repair:
1. use the manifest as the exact commit list
2. rebuild the PR branch in a dedicated worktree
3. validate again
4. only then push or update the PR

Dry-run:
```bash
python3 scripts/syncwheel.py stack rebuild <stack> --worktree <path> --dry-run
```

Apply:
```bash
python3 scripts/syncwheel.py stack rebuild <stack> --worktree <path>
python3 scripts/syncwheel.py stack push <stack>
```

If you are already on the target PR branch and the checkout is clean, you can
use in-place mode instead:

```bash
python3 scripts/syncwheel.py stack rebuild <stack> --in-place
python3 scripts/syncwheel.py stack push <stack>
```

## Phase 5. Repair integration deterministically

Integration is not a mystery branch. It is an ordered replay of declared stacks.
By default this replay is a linear `cherry-pick` of declared commits. If the
manifest sets `integration.strategy` to `merge-stacks`, syncwheel instead
merges each declared stack branch in manifest order with `--no-ff`.

Dry-run:
```bash
python3 scripts/syncwheel.py int rebuild --worktree <path> --dry-run
```

Apply:
```bash
python3 scripts/syncwheel.py int rebuild --worktree <path>
python3 scripts/syncwheel.py int push
```

If you are already on the integration branch and the checkout is clean:

```bash
python3 scripts/syncwheel.py int rebuild --in-place
python3 scripts/syncwheel.py int push
```

Rebuilds create a `backup/<branch>-before-syncwheel-<timestamp>` branch first
when the target branch already exists.

### Manifest self-reference rule

If `.syncwheel/manifest.json` is the source of truth for exact stack commit
ownership, do **not** model a commit that edits that manifest as a normal stack
commit inside the same manifest revision.

Why:
- the manifest would need to name the SHA of the commit that changes the
  manifest itself
- updating the manifest to include that SHA creates another manifest-changing
  commit
- that creates an ownership recursion loop

Stable rule:
- treat manifest edits and syncwheel-version bumps as **control-plane metadata**,
  not as stack-owned product commits
- keep that metadata in an admin checkout/branch or a dedicated maintenance PR
  that is intentionally excluded from `integration.stacks`
- rebuild PR branches and integration from the manifest; then validate again

Practical flow:
1. update `.syncwheel/manifest.json` in a clean admin checkout
2. run `python3 scripts/syncwheel.py reconcile`
3. run `python3 scripts/syncwheel.py reconcile --apply --worktree-root <path>`
4. add `--push --force-with-lease` when the rebuilt managed branches should
   become the shared remote state
5. rerun `check` or `reconcile`
6. commit/publish the manifest update separately if you want it reviewed, but do
   not expect syncwheel to classify that manifest-maintenance commit as a normal
   stack commit in the same manifest revision

This keeps `main-integration` free of stale cherry-picks and avoids infinite
manifest self-classification.

## Phase 6. Validate honestly

Minimum checks:
- `syncwheel.py validate`
- stack-by-stack branch containment
- integration containment
- typecheck/tests if relevant to the repo
- PR publication state if GitHub is in scope

If a repo has known baseline failures unrelated to syncwheel, record them explicitly.

## Phase 7. Report

A useful syncwheel report says:
- what the manifest now declares
- what PR branches were created or rebuilt
- what integration now contains
- what remains intentionally temporary
- what still needs a human decision

## Minimum success criteria

A syncwheel run is successful only when:
- the branch model is explicit
- the manifest is present and valid
- each real integration change maps to a PR stack
- PR branches can be rebuilt from the manifest
- integration can be rebuilt from the manifest order
- unresolved coupling is named explicitly
