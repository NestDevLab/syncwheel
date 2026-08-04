# Syncwheel for AI agents

## Goal

Combine:
- deterministic state recovery from the script
- flexible execution from the AI agent

The script should own **state discovery and rebuild instructions**.
The AI agent should own **judgment, communication, validation, and safe execution**.

## Agent contract

Before editing branches, the agent should run:
```bash
python3 scripts/syncwheel.py status --fetch
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
```

For an active-active version 2 manifest, run `python3 scripts/syncwheel.py
handoff` before planning a handoff or publication. The diagnostic is read-only;
use the coordinated `publish`, `stack push`, or `int push` commands rather than
raw Git pushes. A mergeable lease race still requires an explicit reviewed
`publish --accept-merge`.

The agent should not improvise branch ownership if:
- the manifest is missing
- validation fails because commits are unmapped
- integration contains real work not present in any declared stack

In those cases, the agent should update `.syncwheel/manifest.json` first.
Prefer syncwheel commands over manual JSON edits:

```bash
python3 scripts/syncwheel.py init
python3 scripts/syncwheel.py init --personal alice
python3 scripts/syncwheel.py s new -p alice feature-a --branch pr/alice/feature-a
python3 scripts/syncwheel.py s set -p alice feature-a origin/main..HEAD
```

New manifests require every declared stack to participate in integration. For a
legacy manifest, classify absorbed stacks first, then use `manifest
require-integration --apply` before creating more work.

## Prompt-friendly workflow

A good prompt can be as short as:
- `syncwheel this repo`
- `refresh syncwheel and rebuild the PR branches`
- `recover integration and restack all PRs deterministically`

Given one of those prompts, the agent should:
1. run `status`
2. run `validate`
3. run `plan`
4. summarize the planned actions
5. if authorized, run `stack rebuild` and/or `int rebuild`
6. rerun `validate`
7. report remaining gaps honestly

## Safe execution rules

- Do not run branch-rebuilding commands against a dirty worktree.
- Prefer dedicated worktrees for each rebuild step.
- Treat `stack rebuild` and `int rebuild` as branch mutation unless `--dry-run`
  is used.
- If the repo uses GitHub, validate publication state after branch rebuilds.
- If the manifest and Git disagree, fix the manifest or name the conflict explicitly.
- **A rebuild reconstructs a branch from the manifest's commit projection, NOT from the
  branch's current remote tip.** If the manifest points at a pre-cleanup commit (or a
  range that misses a later fix), `stack rebuild` / `int rebuild` will silently **revert
  that work** — the rebuilt branch force-pushes back to the older state and the cleanup
  disappears. This is a real regression mode, not hypothetical. **Guard against it:**
  after every rebuild/sync/publish, diff the rebuilt branch against the expected
  post-cleanup state and confirm earlier fixes did not regress; keep the manifest current
  with `stack set <id> <rev-or-range>` pointing at the post-cleanup commit BEFORE rebuilding,
  so the projection includes the latest work.

## Suggested human/AI split

Human:
- decides publication policy
- decides whether a stack should exist at all
- approves destructive branch resets when needed

AI:
- keeps the manifest current
- runs deterministic validation
- rebuilds branches in worktrees
- performs honest reporting
- keeps docs and automation in sync

## Recommended report shape

- manifest status
- stacks validated
- branches rebuilt
- integration rebuilt or not
- validation/test outcome
- blockers needing human decision
