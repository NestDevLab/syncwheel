# Manifest

The preferred source of truth is `.syncwheel/manifest.json`.

## Shape

```json
{
  "version": 1,
  "syncwheel_tracking": "git-tracked",
  "syncwheel_worktree_root": "var/syncwheel",
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

- `version` is currently `1`
- `syncwheel_tracking`, when present, must be `git-tracked` or `local-only`
- `syncwheel_worktree_root` defaults to repo-relative `var/syncwheel`
- every stack id must be unique
- every stack branch must be unique
- every declared commit must exist in Git
- `integration.strategy` is optional and defaults to `cherry-pick`
- supported integration strategies are:
  - `cherry-pick`: replay all declared commits into integration as a linear history
  - `merge-stacks`: merge each declared stack branch into integration in manifest order with `--no-ff`
- every persistent integration change should belong to exactly one declared stack unless it is explicit temporary debug work

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
