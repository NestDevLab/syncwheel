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

For an active-active version 2 or 3 manifest, run `python3 scripts/syncwheel.py
handoff` before planning a handoff or publication. The diagnostic is read-only;
use the coordinated `publish`, `stack push`, or `int push` commands rather than
raw Git pushes. A mergeable lease race still requires an explicit reviewed
`publish --accept-merge`.

For deployment channels, inspect contract/list/show/diff first. Every mutation
needs authority and first emits a `channelPlan`; repeat it only with the exact
`--plan-digest ... --apply` and optional stable `--operation-id`. Use `channel
plan` for apply/publish previews, and publish only with the exact lease owned by
`channel publish`. A channel branch is a pinned Git composition, not proof that
an external environment was deployed.

The base is pinned to a full commit and advances only through explicit
`channel refresh`; `baseDrifted` alone is not authority to refresh. Preserve
the exact stack `depends_on` closure/order. A channel-local resolution is bound
to `forPinDigest`; composition edits invalidate it and promotion copies it with
the exact source pins. Shared channels refuse draft-stack publication.
Ephemeral channels may carry drafts, and active-active channels must use the
coordination remote.

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

For an integration-first commit whose future PR is not decided, create a draft
and capture the exact integration commit rather than using `stack add` followed
by a broad reconcile:

```bash
python3 scripts/syncwheel.py stack create exploration --draft \
  --purpose "Classify integration-first work"
python3 scripts/syncwheel.py stack capture-integration exploration <commit>...
```

The command validates that the commit starts from the current integration
projection, validates the revised stack projection before moving a ref, rebuilds
only the named branch with normal recovery/ledger handling, and leaves
integration unchanged. A failed projection leaves the saved manifest unchanged.

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
- Start routine implementation, dependency installation, builds, and tests on integration. Let
  automatic replay choose plumbing or a self-removing temporary worktree. Use `--replay-mode desk`
  only for conflict resolution or to validate a non-empty materialized stack when integration cannot
  safely run it.
- Treat `stack rebuild` and `int rebuild` as branch mutation unless `--dry-run`
  is used.
- Treat each rebuild dry-run as an executable POSIX shell transcript. Current
  non-plumbing commands retain their exact shell-quoted argv; replay environment
  assignments, when present, are only a POSIX prefix to that argv.
- If the repo uses GitHub, validate publication state after branch rebuilds.
- If the manifest and Git disagree, fix the manifest or name the conflict explicitly.
- If a channel plan is stale, replay conflicts, or publication observes unknown
  required state, a post-plan remote change, or lease loss, stop and re-plan.
  Do not force or silently retry.
- If a ref or remote outcome is uncertain, inspect `channel operation show` and
  use digest-bound `channel operation reconcile`. It appends observed terminal
  evidence and never retries the mutation.
- Treat an operation cancelled before its authoritative boundary as terminal;
  an interruption at or after that boundary needs reconciliation.
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
- rebuilds branches through automatic replay, using a persistent worktree only
  when one is deliberately needed
- performs honest reporting
- keeps docs and automation in sync

## Recommended report shape

- manifest status
- stacks validated
- branches rebuilt
- integration rebuilt or not
- validation/test outcome
- blockers needing human decision
- channels planned/applied/published/closed, receipt digests, and any separate
  deployment evidence (or the explicit absence of it)
