# Syncwheel deterministic model

## Why Git alone is not enough

Git can answer questions like:
- what commits are on this branch?
- does branch A contain commit X?
- how does branch A differ from branch B?

Git cannot natively answer this with certainty:
- “which commits belong to PR stack `foo`?”

That becomes deterministic only when the repo declares that mapping explicitly.

## What "PR stack" means here

In syncwheel, a PR stack is a logical change stream that should be reviewable as one PR branch:
- one stack id (for example `feature-a`)
- one `pr/*` branch mapped to that stack
- one declared commit list that defines stack membership

This avoids guessing ownership from branch names or memory.

## What a deployment channel means here

A channel pins the full revision of its symbolic base plus an ordered branch
composition of selected stacks. Each entry pins:

- the stack id and branch;
- the full observed stack branch revision;
- the exact ordered full commit list.

Those pins make the channel rebuildable even when the stack branches later
move. A refresh is explicit; the channel never follows a stack tip implicitly.
It is distinct from the full integration projection and from any external
environment deployment.

## Preferred source of truth

Create:
- `.syncwheel/manifest.json`

Bootstrap template:
- `examples/manifest.example.json`

Suggested shape:

```json
{
  "version": 1,
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "fork",
    "base_branch": "main",
    "base_ref": "origin/main"
  },
  "integration": {
    "branch": "main-integration",
    "base": "origin/main",
    "stacks": ["stack-a", "stack-b"]
  },
  "stacks": [
    {
      "id": "stack-a",
      "branch": "pr/stack-a",
      "base": "origin/main",
      "target_remote": "origin",
      "target_branch": "main",
      "integration_branch": "main-integration",
      "commits": ["abc1234", "def5678"]
    }
  ]
}
```

## Semantics

- `defaults.base_ref`: canonical base used to rebuild PR and integration branches
- `integration.stacks`: replay order for the integration branch
- `stacks[].branch`: the PR branch that should contain exactly that stack’s commits on top of its base
- `stacks[].commits`: the declared commit list for that logical change set
- `channels[].composition`: selected stack pins in channel replay order
- `channels[].baseRevision`: exact base commit used by materialization;
  `channel refresh` advances it deliberately

## Operational rule

If a real fix or feature exists on integration, it should appear in exactly one declared stack unless it is explicitly temporary debug work.

## What the script can verify

Given the manifest, `syncwheel.py` can verify deterministically:
- whether each declared commit exists
- whether the PR branch exists
- whether the PR branch contains the declared commits
- whether integration contains the declared commits
- whether integration references unknown stacks
- whether replaying a declared commit onto its unchanged base preserves its SHA
- whether a channel's pinned stack revision and exact commit list still match
  its declared observation
- whether a plan digest still matches the observed base, composition, local
  branch, remote ref, and manifest revision before apply or publish

## What still remains heuristic

These remain outside pure Git determinism unless you add more metadata:
- whether a commit should be split into multiple PRs
- whether independently created commits with different SHAs are conceptually the same fix
- whether an integration-only reconciliation commit should become public
- whether publishing a channel should trigger or has triggered a deployment

The last point belongs to CI/CD. Syncwheel proves a channel Git revision and its
plan-bound receipt, not environment state.

For those cases, the manifest should be updated deliberately.
