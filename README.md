# syncwheel

Deterministic fork/upstream/integration maintenance for Git repositories.

`syncwheel` is a small CLI plus a documentation model for teams that:
- do day-to-day work on an `integration/*` branch
- publish clean `pr/*` branches toward an original upstream repository
- want AI agents and shell automation to rebuild those branches repeatably

## Why it exists

Git can tell you which commits are on a branch. It cannot natively tell you, with certainty, which commits belong to a logical PR stack unless you declare that mapping explicitly.

`syncwheel` makes that mapping explicit in `.syncwheel/manifest.json`, then validates and materializes branch state from it.

## Core model

Unless a repository documents a different rule:
- normal development happens on `integration/*`
- every persistent change on integration should also belong to a `pr/*` stack
- `pr/*` branches are review surfaces for upstream PRs
- long-lived integration-only product code is drift and should be surfaced

## Install

No package install is required. The tool is a single Python script.

Requirements:
- Python 3.11+
- Git

## Quick start

### 1. Bootstrap a manifest

```bash
python3 scripts/syncwheel.py init --stdout > .syncwheel/manifest.json
```

Or copy the example:

```bash
mkdir -p .syncwheel
cp examples/manifest.example.json .syncwheel/manifest.json
```

### 2. Inspect current state

```bash
python3 scripts/syncwheel.py status --fetch
```

### 3. Validate manifest against Git

```bash
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
```

### 4. Rebuild one PR branch from the declared stack

Dry run:

```bash
python3 scripts/syncwheel.py materialize-pr feature-a --worktree ../wt-pr-feature-a
```

Apply:

```bash
python3 scripts/syncwheel.py materialize-pr feature-a --worktree ../wt-pr-feature-a --apply
```

### 5. Rebuild integration from declared stack order

Dry run:

```bash
python3 scripts/syncwheel.py materialize-integration --worktree ../wt-integration
```

Apply:

```bash
python3 scripts/syncwheel.py materialize-integration --worktree ../wt-integration --apply
```

## Files

- `scripts/syncwheel.py`: main CLI
- `scripts/syncwheel-status.sh`: small compatibility wrapper
- `docs/`: human-readable workflow docs and guides
- `examples/manifest.example.json`: starter manifest
- `tests/`: unit tests and fixture repositories

## Documentation map

- `docs/workflow.md`: concise workflow model
- `docs/core-procedure.md`: deterministic recovery procedure
- `docs/branch-model.md`: branch role model and safety defaults
- `docs/deterministic-model.md`: manifest semantics and validation contract
- `docs/ai-agents.md`: short AI behavior contract
- `docs/agent-procedure.md`: extended AI execution guidance
- `docs/workflow-longform.md`: long-form practical workflow guide
- `docs/public-article.md`: narrative article version for broader audiences

## CLI summary

```bash
python3 scripts/syncwheel.py --help
python3 scripts/syncwheel.py init --help
python3 scripts/syncwheel.py status --help
python3 scripts/syncwheel.py validate --help
python3 scripts/syncwheel.py plan --help
python3 scripts/syncwheel.py materialize-pr --help
python3 scripts/syncwheel.py materialize-integration --help
```

## AI agent usage

Agents should not infer stack ownership from memory when the repository is meant to be maintained via `syncwheel`.

Recommended sequence:
1. `status --fetch`
2. `validate`
3. `plan --json`
4. update the manifest if reality changed
5. `materialize-pr` and/or `materialize-integration`
6. rerun `validate`
7. report remaining drift honestly

See [docs/ai-agents.md](docs/ai-agents.md).

## License

MIT
