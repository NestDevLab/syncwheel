# syncwheel

Deterministic fork/upstream/integration maintenance for Git repositories.

`syncwheel` is a small CLI plus a documentation model for teams that:
- publish clean `pr/*` branches toward an original upstream repository
- optionally keep an `integration/*` branch for combined runtime testing
- want AI agents and shell automation to rebuild those branches repeatably

`integration/*` is **recommended**, not mandatory.

You can use syncwheel in two modes:
- **PR-only mode**: manage and validate PR stacks without an integration branch
- **Integration mode**: also maintain a combined branch to test multiple in-flight PRs together

## How syncwheel works

The system has four pieces:
- **base branch** (`upstream/main` or similar)
- **PR stacks** (logical units of work mapped to `pr/*` branches)
- **optional integration branch** (`integration/*`) for combined runtime testing
- **manifest** (`.syncwheel/manifest.json`) as the declared ownership model

A PR stack in syncwheel means:
- one logical change stream (for example `feature-a`)
- one PR branch (for example `pr/feature-a`)
- one explicit commit list in the manifest

This is what makes validation and rebuild deterministic.

## Who this is for

`syncwheel` is for teams or maintainers who have at least one of these conditions:
- active upstream + fork workflow
- multiple PR branches that must stay clean while development continues
- an `integration/*` branch used as day-to-day runnable state
- need for repeatable branch recovery that does not depend on memory

## Who this is not for

`syncwheel` is usually overkill when:
- you ship directly from one branch with short-lived PRs only
- your repo has no integration branch and no stacked branch maintenance
- your process does not need deterministic rebuilds from a declared manifest

## Three ways to use syncwheel

1. **Guide-first (manual execution)**  
   Use the docs as an operating playbook and run Git steps manually. This is possible, but cognitively heavier and easier to get wrong in complex branch graphs.

2. **Script-assisted (human-operated)**  
   Use the CLI for discovery, validation, and materialization, while a human decides what to run and when. This is a strong middle ground once the team knows the model well.

3. **AI-operated (recommended)**  
   Let an AI agent run the syncwheel flow through prompts, with a human supervising intent and approval boundaries. In practice this gives the best speed/consistency balance for ongoing maintenance.

## Core model

Unless a repository documents a different rule:
- normal development happens on `integration/*`
- every persistent change on integration should also belong to a `pr/*` stack
- `pr/*` branches are review surfaces for upstream PRs
- long-lived integration-only product code is drift and should be surfaced

## System flow (visual)

The `integration/*` branch is where combined runtime reality lives when enabled.

Use it to:
- run and test multiple in-flight changes together
- continue development without waiting for each upstream PR to merge
- detect cross-PR conflicts early, before review-time surprises

In syncwheel, synchronization is **bidirectional by design** (through explicit materialization):
- from declared stacks -> rebuild integration (`materialize-integration`)
- from declared stacks -> rebuild each PR branch (`materialize-pr`)

This gives you parallel PR development with a shared, testable branch that stays traceable.

```mermaid
flowchart LR
    U[upstream/main]
    I[integration/main]
    P1[pr/feature-a]
    P2[pr/feature-b]
    P3[pr/hotfix-c]
    M[.syncwheel/manifest.json]

    U --> P1
    U --> P2
    U --> P3

    M --> I
    M --> P1
    M --> P2
    M --> P3

    I <--> P1
    I <--> P2
    I <--> P3
```

Practical meaning of the arrows:
- integration can be rebuilt from the declared stack order
- PR branches can be rebuilt from the same declared commit ownership
- the manifest is the source of truth that keeps both sides aligned

## Install

No package install is required. The tool is a single Python script.

Requirements:
- Python 3.11+
- Git

## Installation and adoption modes

1. **Global toolkit (recommended)**
   - Clone `syncwheel` once in a stable location.
   - Run it against target repos via `-r/--repo` using either paths or aliases.
   - Best when you want one central install to keep updated.

2. **Git submodule**
   - Add `syncwheel` as a submodule inside each target repo.
   - Good when each project must pin an explicit syncwheel version.

3. **Vendored script**
   - Copy `scripts/syncwheel.py` into a project.
   - Fastest for experiments, but updates are manual.

## Repo aliases

You can register repo aliases and keep commands short.

```bash
python3 scripts/syncwheel.py repo add yalc ~/code/nestjs-yalc
python3 scripts/syncwheel.py repo ls
python3 scripts/syncwheel.py status -r yalc --fetch
python3 scripts/syncwheel.py repo rm yalc
```

`-r/--repo` accepts both:
- a filesystem path
- a registered alias

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
