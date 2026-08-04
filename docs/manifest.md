# Manifest

The preferred source of truth is `.syncwheel/manifest.json`.

## Shape

```json
{
  "version": 1,
  "syncwheel_tracking": "git-tracked",
  "syncwheel_worktree_root": ".syncwheel/wt",
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "fork",
    "base_branch": "main",
    "base_ref": "origin/main"
  },
  "integration": {
    "branch": "integration/project-stack",
    "base": "origin/main",
    "strategy": "merge-stacks",
    "stacks": ["feature-a", "feature-b"]
  },
  "stacks": [
    {
      "id": "feature-a",
      "branch": "pr/feature-a",
      "base": "origin/main",
      "target_remote": "origin",
      "target_branch": "main",
      "integration_branch": "integration/project-stack",
      "commits": ["abc1234", "def5678"]
    }
  ]
}
```

## Create manifests with commands

Create a shared manifest:

```bash
python3 scripts/syncwheel.py init
```

By default this creates a shared integration branch named `main-integration`.
Use `--integration-branch <name>` when a repository wants a different shared
integration branch name:

```bash
python3 scripts/syncwheel.py init --integration-branch integration/team-stack
```

Persist the repo's Syncwheel tracking policy:

```bash
python3 scripts/syncwheel.py repo tracking status
python3 scripts/syncwheel.py repo tracking set git-tracked --apply
python3 scripts/syncwheel.py repo tracking set local-only --apply
```

Use `git-tracked` when `.syncwheel/manifest.json` is meant to be committed as
the repo's shared coordination contract. Use `local-only` when Syncwheel metadata
must stay out of Git; this mode writes local excludes through `.git/info/exclude`,
not `.gitignore`.

### Version 2 active-active manifests

The sample above is a valid legacy version 1 manifest. Version 2 adds a required
`coordination` block for durable, multi-device publication:

```json
{
  "version": 2,
  "syncwheel_tracking": "git-tracked",
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "origin",
    "base_branch": "main",
    "base_ref": "origin/main"
  },
  "coordination": {
    "mode": "active-active",
    "id": "default",
    "remote": "origin",
    "state_branch": "syncwheel/state/default",
    "gc": {
      "worktree_grace_days": 7,
      "backup_retention_days": 30,
      "backup_keep": 2
    }
  }
}
```

Create this form for a new shared repository only when the publication remote
is configured:

```bash
syncwheel init --syncwheel-tracking git-tracked --publication-remote origin
```

If the remote is unavailable, initialization stops instead of silently turning
coordination off. Use `--no-coordination` only for an intentional persisted
opt-out. Existing version 1, `local-only`, and disabled manifests opt in
explicitly:

```bash
syncwheel coordination init --remote origin --apply
syncwheel coordination disable --apply
```

`coordination.remote` must equal `defaults.publication_remote`, and its state
branch is always `syncwheel/state/<coordination-id>`. See
[active-active-coordination.md](design/active-active-coordination.md) for the
publication and recovery protocol.

Create a personal local manifest:

```bash
python3 scripts/syncwheel.py init --personal alice
```

This writes `.syncwheel/manifests/alice.local.json` and sets the integration
branch to `integration/alice/main`.

Use the personal manifest with the short `--personal` flag:

```bash
python3 scripts/syncwheel.py check -p alice
```

Or set the personal manifest as the default for the current clone:

```bash
python3 scripts/syncwheel.py use alice
python3 scripts/syncwheel.py check
python3 scripts/syncwheel.py use --shared
```

Create stack entries through the CLI:

```bash
python3 scripts/syncwheel.py stack create feature-a --branch pr/feature-a -u
python3 scripts/syncwheel.py stack set feature-a origin/main..HEAD
```

`-u` is the short form of `--include-in-integration`.

## Rules

- `version` is `1` for legacy manifests or `2` for manifests with a required
  `coordination` block
- `syncwheel_tracking`, when present, must be `git-tracked` or `local-only`
- `syncwheel_worktree_root` defaults to repo-relative `.syncwheel/wt`
- version 2 `coordination.mode` is `active-active` or persisted `disabled`
- version 2 coordination must use the same named remote as
  `defaults.publication_remote`
- every stack id must be unique
- every stack branch must be unique
- every declared commit must exist in Git
- `commits` remain the source projection used to rebuild a stack branch
- `integration_commits`, when present, is the resolved projection used only for
  integration. Record it after a conflict creates new integration commits; it
  prevents Syncwheel from treating those commits as source-branch commits.
- `integration.strategy` is optional and defaults to `cherry-pick`
- supported integration strategies are:
  - `cherry-pick`: replay all declared commits into integration as a linear history
  - `merge-stacks`: merge each declared stack branch into integration in manifest order with `--no-ff`
- every persistent integration change should belong to exactly one declared stack unless it is explicit temporary debug work
- a commit changing only `.syncwheel/manifest.json` is treated as integration
  control-plane metadata, not an unclassified product change

## What validation checks

`syncwheel.py validate` checks:
- manifest structure
- existence of integration base ref
- existence of PR branches
- existence of declared commits
- whether PR branches contain declared commits
- whether integration contains declared commits
- whether integration references unknown stacks
- whether integration contains non-merge commits that are not declared in any stack

Unmapped integration commits are reported as warnings plus a
`classify_integration_commits` plan action. The tool can identify the commits,
but a human or AI agent still needs to decide which stack owns each change.

### Resolved integration projections

When a declared stack commit conflicts during integration, resolve the conflict
on the integration branch and record the resulting commit separately. This keeps
the source branch immutable while allowing deterministic validation and rebuilds
of integration:

```bash
syncwheel stack resolve-integration feature-a <resolved-commit>
```

The command accepts one or more commits already contained by the integration
branch. It updates `integration_commits` without changing `commits`; stack
rebuilds still use the latter, while integration rebuilds use the former.
When an obsolete source commit is already absorbed by the integration base or
another resolved stack, use `--empty` explicitly rather than inventing a commit.
