# Manifest

The preferred source of truth is `.syncwheel/manifest.json`.

## Shape

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
    "branch": "integration/project-stack",
    "base": "origin/main",
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

## Rules

- `version` is currently `1`
- every stack id must be unique
- every stack branch must be unique
- every declared commit must exist in Git
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
