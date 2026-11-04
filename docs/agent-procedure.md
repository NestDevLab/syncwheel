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

Before the first use of each mutating subcommand in a session, and after an
upgrade or syntax error, run its exact nested `--help`. Treat the installed
help as authoritative for flags and defaults; do not infer them from memory.

After delivery, close by proof: use `stack close <id> --reason merged` only
when the declared commits are ancestors of the target; use `--reason absorbed`
for squash/rebase delivery. The latter fetches the delivery tip and verifies
the final content, so normal squash cleanup does not need `--force`. Under
active-active coordination a close publishes only the removal of that stack from
the published state; another stack's unpublished local change neither blocks it
nor gets published by it. When a projection replay stops, the error names the
stack and commit. It suggests `stack close <id> --reason absorbed` only when the
whole stack is proven to be in the base (for example after a squash merge);
otherwise it names the conflicting paths, the other stack involved, or Git's own
first error line, with the desk rebuild command.

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

## Publish draft updates before promotion

After adding commits and rebuilding integration, run `syncwheel publish` before
`syncwheel stack promote <stack>`. Active-active promotion proves a branch/state
transition against published coordination metadata; unpublished draft changes
must be published first.

Install the candidate CLI (`python3 -m pip install .`) so guard hooks can resolve
a stable executable. Run `python3 scripts/cli-dogfood.py` for a disposable local-remote delivery and
second-clone resume check. Every subprocess is timed and bounded; its evidence
directory is printed at startup.

### Tracked control recovery after an interrupted local replay

An ephemeral replay may reset a checked-out integration branch to a tree carrying
an older tracked manifest. The normal persistence checkpoint accepts only the
manifest content proved by that replay, including manifests with omitted
defaults; an independent checkout edit still fails closed.

For a legacy interruption without a persistence intent, inspect the native backup
ref and preview the bounded local recovery:

```bash
syncwheel manifest recover-control --from-backup backup/integration/example-before-syncwheel-TIMESTAMP
```

Review its backup commit, current checkout/index/refs, replay proof and digest.
Apply that exact plan with `--apply --plan-digest DIGEST --reason "Restore reviewed replay control"`.
The command requires the canonical git-tracked delivery manifest, a native backup
for the same integration branch, and an independently reproduced product tree.
Active-active coordination, edited manifests, staged/unrelated work and unrelated
ignore changes are refused. The plan pins a manifest-only control object to the
reviewed integration parent. Publication reuses the existing expected-tip CAS,
durable control intent and guarded checkout alignment; it never stages late file
contents through an ordinary metadata commit. A proved native managed-ignore
rewrite is preserved as unstaged work, not included in this recovery commit.
Acquire that owned rewrite separately through the normal scoped native authoring
procedure. Product commits, source branches, private local policy, recovery refs
and foreign worktrees are preserved. Replan on drift; never
use a manual reset, manifest edit or a bypass of the transaction guard.

Recovery does not publish or attest validation. Verify the restored recipes and
source ownership, then run the normal integration checks and exact-head delivery
plan. An already restored input is a no-op under a freshly observed plan.
Changes observed after the control CAS are held with an error and the existing
control receipt for inspection. Concurrent content remains in the checkout;
this is not a claim that the whole recovery completed or permission for a blind
retry, automatic rollback, or unattended recovery.

An interrupted explicit recovery is quarantined in its durable intent. Ordinary
manifest writers, integration commands and direct persistence consumers refuse
it before alignment, receipts or metadata writes, including when the control CAS
already succeeded. They do not execute the requested new operation. The guarded
control object uses a distinct deterministic identity so older consumers reject
it rather than applying their automatic recovery rules.

Obtain a fresh `manifest recover-control --from-backup BACKUP` plan to continue.
It verifies the original pending object/parent and the same backup proof, and
includes the pending intent in its digest. Explicit apply may finish that exact
operation without adding another commit. Foreign ignore edits, product commits,
staged input or an altered proposal remain held for their owner; a fresh plan
does not waive those refusals. An interruption before checkout alignment may
leave a staged manifest, which this bounded command deliberately refuses rather
than repairing automatically. Inspect that retained state under the existing
supervised recovery procedure before replanning. No implicit restart is certified.

### Empty integration-only desk steps

A stopped integration-only cherry-pick can be reviewed through the ordinary
`stack resolve-integration STACK --worktree DESK` command when the desk reports
an empty step. Syncwheel verifies the exact pending SHA, unchanged HEAD, clean
index and worktree, and an independent replay that produces the same tree.
Capture retires only that contribution, preserves source branches and sibling
steps, and records its exact no-op proof in the normal source ledger. Resume
`int rebuild --replay-mode desk --worktree DESK --reason "Continue reviewed replay"`
to execute the remainder. Legacy ready receipts with the same stopped pick can
be resumed by the same desk rebuild command.

Changed source inputs, pending operation identity, staged or unstaged content,
and untracked work fail closed. Unknown Git failures are not classified from
error-message text. A later normal rebuild verifies the retirement before
carrying historical integration commits into its existing ancestry proof. An
interrupted capture uses its write-ahead receipt to complete the same source
ledger transition before continuing.

### Historical auto-merges after absorption

Normal ancestry reconciliation accepts a historical auto-merge of a two-parent
managed source endpoint only when the existing coordination chain proves an
`absorbed` close, verifies both control-manifest digests, and binds a declared
source parent to that exact managed tip. It still reconstructs the auto-merge
and compares its entire historical tree. Abandoned, superseded or promotion-only
ownership does not authorize this additional route; the control-only fallback
is unchanged. The proof works from a fresh clone without a local close ledger.
