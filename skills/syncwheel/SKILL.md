---
name: syncwheel
description: Use Syncwheel for deterministic, multi-agent-safe Git maintenance — PR branches, stacked PRs, draft stacks, integration branches, and pinned deployment-channel branches, all from a single checkout. Use whenever you are about to create a PR branch, compose a deployment channel, manage a fork/upstream/integration or PR-stack workflow, own a commit before you know which PR it belongs to, rebuild or publish a stacked PR, or coordinate Git work on a repo that other people or agents may touch concurrently or that contains a `.syncwheel/` directory. Also covers the decision of whether to commit the Syncwheel manifest (own repo) or keep it untracked (external contribution).
allowed-tools: [Bash]
metadata:
  version: "1.0"
---

# Syncwheel

Syncwheel makes Git maintenance **deterministic and safe when many agents touch the
same repositories**. Branch ownership, PR stacks, and the integration branch are
described in a manifest; Syncwheel reconciles Git to match it. Plain manual
`git`/worktree surgery is the exception path, used only when Syncwheel is
unavailable, blocked, or cannot express the needed recovery.

## Ratified working rules (read this first)

These four are ratified operating rules (MGT-0206), not suggestions. A real incident
happened because an operator resolved a rebuild conflict with raw git instead of rule 2 —
the mechanism already existed; only the visibility was missing. Read rule 2 twice.

1. **Never author in the primary checkout.** It stays on `manifest.integration.branch` as
   the shared test projection. Open a governed lane instead:
   ```bash
   syncwheel worktree open <lane> [--into <stack>] [--full]
   ```
   As of 0.42.4, where the guard hooks are installed, this is enforced: a manual commit on
   the primary, or an unauthorized move of the integration ref there, is refused, and every
   mutating Syncwheel command refuses to run while the primary has tracked changes. Arm the
   guard once per clone:
   ```bash
   syncwheel hooks install --apply
   ```
   Opt out only with a reasoned, ledgered, clone-local disable:
   ```bash
   syncwheel hooks remove --disable --reason "<why>" --apply
   ```

2. **Never resolve a replay conflict with raw git.** A plumbing replay never descends
   silently into a conflict; it names the exact retry:
   ```bash
   syncwheel stack rebuild <id> --replay-mode desk
   ```
   Resolve inside that worktree through the manifest, not through a manual merge commit:
   ```bash
   syncwheel stack absorb <stack> [<path>...|--staged]
   syncwheel stack resolve-integration <stack> <resolved-commit>...
   ```
   A manual `git merge` or `git commit` on the integration branch during a conflict is
   outside Syncwheel's bookkeeping. The next `stack rebuild` / `int rebuild` reconstructs
   the branch from the manifest's own commit projection, has no record that a manual
   resolution ever happened, and silently drops it.

3. **Integration composition is a declared, visible operation.** Before testing on
   integration, or before blaming your own code for something that looks broken there,
   check what is actually integrated:
   ```bash
   syncwheel int show
   ```
   Add a stack by declaring it — required-membership manifests include every declared stack
   in `integration.stacks` by default — then rebuild:
   ```bash
   syncwheel stack create <id> [<commit-or-range>...] [--draft]
   syncwheel int rebuild --reason "<why>"
   ```
   Remove one by closing it, then rebuild:
   ```bash
   syncwheel stack close <id> --reason "<why>"
   syncwheel int rebuild --reason "<why>"
   ```

4. **Every mutating command carries `--reason`; it is mandatory in `ai-managed`
   repositories.** Pass it always, not only on the commands that already refuse to run
   without one (`int rebuild` in an ai-managed repo, `hooks remove --disable`,
   `worktree release`, `coordination provenance reset`, among others). The reason lands in
   the append-only ledger together with the actor and the exact command:
   ```bash
   syncwheel ledger show
   ```

### When something looks wrong

Do not force a push, do not hand-edit `.syncwheel/manifest.json` or the coordination
state, and do not fall back to raw git. Re-observe, then use the exact remedy the failing
command names.

For a managed ref that disagrees with the coordination state, `syncwheel coordination
repair` is the family of named remedies, always plan-first then `--apply --plan-file
<plan>`:

| Situation | Backend |
|---|---|
| The ref is otherwise correct, only the recorded tip is wrong (ref repair) | default — `syncwheel coordination repair --ref <ref> > plan.json` |
| Recorded and observed commits differ only in shape, same tree | `--freeze-backend tree-equivalent-state-cas` |
| The ref advanced through an exact, reviewed fast-forward | `--freeze-backend fast-forward-state-cas` |
| A state was published before 0.42.2 and carries the legacy manifest-digest form | `--freeze-backend state-digest-migration` (or let the next publish/push/compose migrate it automatically) |

Each backend proves ancestry/tree/ownership before writing and only ever appends
coordination state — none of them touch the managed branch itself.

## When to use (Syncwheel-first)

**First, detect the regime.** Before branch, worktree, integration, PR, recovery,
or handoff work in any Git repo, check whether it is Syncwheel-managed: a
`.syncwheel/` directory or manifest is present, or a workspace/project guide says
so. When unsure, run `syncwheel status`. If it is managed — or the repo is shared,
fork/upstream, or touched by multiple agents — it is Syncwheel-first: do not reach
for manual `git` branch/worktree/integration surgery as the default path.

Reach for Syncwheel **before** any of these, not after:

- editing or handing off work in a Git repository that is shared, managed by
  Syncwheel, or likely to be touched by multiple agents
- creating a PR branch or a stack of dependent PRs
- working in a repo that has a `.syncwheel/` manifest, or a
  fork/upstream/integration/PR-stack workflow
- committing on integration before you know which PR owns the change
- rebuilding or pushing PR branches / integration
- coordinating with other agents who may rebase the same branches

If the task involves a Git repo that has shared coordination risk, prefer
Syncwheel even when the visible edit is small. A local file change is not a
complete handoff by itself: before final response, report the Git state, required
checks, and the commit/push/PR decision or the explicit reason delivery remains
local.

## The contract

When the manifest declares `repository_mode: "journal"`, use only
`syncwheel journal status`, plan-first `journal snapshot` / `journal publish`,
and `journal schedule`. Add `--apply` only with mutation authority. Journal mode
forbids stack, integration, reconcile, sync, and delivery publish commands; its
publisher stops on remote-ahead, divergence, or lease loss without history surgery.

A deployment channel pins an exact base revision plus an ordered branch
composition of exact stack revisions, commit lists, base provenance, and
dependency closure/order. It is separate from a stack, from the full integration
projection, and from an actual environment deployment. Every mutation previews
a `channelPlan` and requires its exact `--plan-digest` with `--apply`. Publish
only through the exact lease owned by `channel publish`, and never claim that
branch publication proves an environment was deployed.

The script owns: repo-state discovery, manifest validation, deterministic branch
and integration reconstruction. The agent owns: judgment, communication,
project-specific validation after a rebuild, and safe execution.

The primary Git worktree stays on `manifest.integration.branch` as the shared test projection; do
not author or commit there. Open a governed lane before authoring. Rebuilds no longer create a
worktree: replay runs through Git plumbing where available, or a temporary worktree that is removed
before the command returns. A clean, bounded integration operation may switch the primary checkout
temporarily, but must restore and verify the integration branch before completion. Treat any other
primary-checkout mismatch as a validation error and blocked handoff.

Desk is an escalation/validation surface, not routine authoring: begin routine implementation,
dependency installation, builds, and tests on integration. Use `--replay-mode desk` only to resolve a
conflict or validate a non-empty materialized stack when integration cannot safely run it; it is never
a side effect of rebuilding.

When the primary checkout is explicitly owned by another agent and authoring
must continue, use the explicit governed fallback instead of manual worktree
surgery:

```bash
syncwheel worktree open <lane> [--into <existing-stack>] [--full]
```

The default light lane is for editing and committing only. It receives no
dependency provisioning; use `--full` only for an explicitly necessary
dependency, build, test, or debugging surface. This is not a security sandbox,
so do not defeat the light-lane boundary with raw install/test commands. The
lane is clone-local and bounded to four active entries. After its commits are
owned through the existing `stack create`, `stack add`, or
`stack capture-integration` flow, Syncwheel stores a local recovery ref and
reaps only a clean lane. A missing lane with an expired lease or a known-dead
local owner is eligible even if its retained registry path is outside the
configured root; a dirty, unknown, or current-directory lane stays visible and
requires explicit recovery. Check governed-worktree warnings before a mutating
lifecycle command.

To retire a named dead or abandoned lane, first preview and then explicitly
apply the release:

```bash
syncwheel worktree release <lane> --reason "<why>"
syncwheel worktree release <lane> --reason "<why>" --apply
```

The preview does not write. Applying stores a recovery ref for an existing lane
branch tip, removes the registry record, and appends a ledger event. It refuses
an existing dirty worktree and names the recovery remedy. `gc --apply` reaps
eligible expired lanes even when active-active coordination is disabled.

## Locate the CLI

Syncwheel is available as the PATH `syncwheel` command. Install it with:

```bash
uv tool install "git+https://github.com/NestDevLab/syncwheel"   # production
uv tool install --editable <local-clone>                          # development
syncwheel self update                                             # keep current
```

If Agentwheel is available, install this skill into the local Codex runtime for
the target repo:

```bash
agentwheel install github:NestDevLab/syncwheel --adapter codex --local --target-root <repo> --skill syncwheel
syncwheel self status
```

If the PATH binary is not available (legacy host or vendored install), fall back to the checkout pointer:

```bash
SW="python3 ${SYNCWHEEL_REPO:?set SYNCWHEEL_REPO to the syncwheel checkout}/scripts/syncwheel.py"
$SW --version
$SW self status
```

Always run Syncwheel **against the target repo**: pass `-r <repo-path-or-alias>`
or run from inside the target repo's worktree.

## Safe lifecycle (always dry-run first)

```bash
syncwheel status --fetch          # discover real Git state
syncwheel validate                # manifest vs Git
syncwheel plan --json             # deterministic action plan
syncwheel reconcile               # dry-run reconcile (no writes)
syncwheel reconcile --apply --worktree-root <path>   # apply, only after the plan is understood
syncwheel reconcile --apply --worktree-root <path> --push   # publish shared branches
syncwheel check                   # re-verify
```

When a version 2 or 3 manifest has `coordination.mode: "active-active"`, add a
read-only handoff before planning mutations or publishing from a different
device/agent:

```bash
syncwheel handoff
```

Use `publish`, `stack push`, or `int push` for that manifest's managed refs.
They publish atomic state with exact leases; do not substitute a raw `git push`.
Install the plan-first managed-ref guard in each clone with `syncwheel hooks
install`, review the reported hook/chaining path and digest, then apply with
`syncwheel hooks install --apply`. The guard is composable and catches accidental
raw pushes, but `--no-verify` remains a local bypass and the hook is not a security
boundary. A required guard is reported as pending until this explicit installation;
normal Syncwheel commands do not install it implicitly. Once installed, an
unresolvable stable CLI or an altered/partial hook bundle makes the guard fail closed
and `hooks status` reports `degraded` with the cause. Syncwheel's guard runs before
every chained user hook, both execute, and either failure rejects the operation.
Use the same `--personal` or `--manifest` selector for `hooks install`, `status`, and
reasoned removal. The clone has one effective guard target; another selected profile
or a renamed integration branch is degraded until an installation with the same
selector and `--reason "..."` retargets it. Retargeting appends an intent with actor,
old target, new target, and reason to the selected ledger before changing
`guard.json`.
If a publication reports a mergeable race, review `handoff` and use
`publish --accept-merge` only after the user explicitly accepts that disjoint
stack merge.

Short aliases are preferred when they make commands easier to scan:

```bash
syncwheel status -f -j
syncwheel plan -j
syncwheel reconcile -a -W .syncwheel/wt
syncwheel reconcile -a -P -W .syncwheel/wt
syncwheel repo tracking set git-tracked -a
```

For a channel, start with read-only inspection:

```bash
syncwheel channel list
syncwheel channel show <id>
syncwheel channel diff <id>
syncwheel channel contract
syncwheel channel operation list
syncwheel channel plan <id>
```

`channel create` claims a new ref and does not adopt an existing local or
remote branch. It also refuses base, integration, stack source/target, and
coordination-state refs. Choose a fresh branch name after an explicit close.

For `create`, `add`, `remove`, `replace`, `refresh`, `promote`, `resolve`,
`apply`, `publish`, and `close`, extract the preview's `planDigest`, then repeat
the same command with `--plan-digest <digest> --apply`. Supply the same optional
stable `--operation-id` to both calls; if omitted, Syncwheel derives one from
the plan. Operation ids do not contribute to the plan digest.
Materialization/publication previews come from `channel plan <id>`.

Stop on a stale observation, replay conflict, unknown required state, post-plan
remote change, or lease loss. Re-observe and re-plan; do not substitute raw Git
or force publication. Channel expiry is only metadata, so cleanup remains an
explicit `channel close` operation.

If a local-ref or remote outcome is uncertain, inspect `channel operation show`
and preview/apply `channel operation reconcile`. Reconciliation observes and
appends a terminal receipt; it never retries the original mutation. Operations
record started/prepared/receipt evidence and end as `succeeded`, `failed`,
`partial`, `unknown`, or `cancelled`.

Only `channel refresh` advances a channel's base pin. Shared channels refuse
draft-stack publication; ephemeral channels may use drafts. Under active-active
coordination a channel must use the coordination remote.

Use `channel resolve <id> --revision <full-sha>` for a channel-local snapshot
commit directly on the pinned base, or `--clear` to remove it. The resolution is
bound by `forPinDigest`; add/remove/replace/refresh invalidate it, while promote
copies a valid resolution with the exact source pins. Git 2.38+ materializes
through plumbing; older Git uses a self-removing temporary worktree.

`syncwheel spoke ...` is a readable alias for `syncwheel stack ...` when the
wheel metaphor helps, but the manifest field remains `stacks`.

Never run a built-in mutation while the shared primary checkout is dirty: it stops before side effects
and names `worktree open` or `stack capture-integration` as the remedy. Read-only commands remain
available and emit a yellow TTY warning with the dirty-file count; treat the changes as foreign to the
invoking user. Use `--dry-run` on rebuild/push commands. If the manifest and Git disagree, fix the
manifest or call out the conflict — do not claim a repo is aligned while integration and PR branches
still differ. The named recovery commands remain executable while it is dirty. The
primary guard is fail-closed and uses a single-use internal nonce; `hooks status`
reports a degraded bundle and `hooks install --apply` repairs it explicitly. Guard
state comes only from atomic `guard.json` under the Git common directory. Re-enable
state precedes hook installation; reasoned disable is ledgered before hook removal.
Every state read validates the branch, boolean enable flag, and required disable
reason; missing, non-UTF-8, or invalid state fails closed, reports degraded, and is
repairable by explicit installation. Nonces bind PID
plus process-start identity, cleanup preserves other live processes, and stale
malformed files are audit-recorded before removal. Mutation/read-only/remedy behavior
is declared once in the entrypoint registry, including internal writers, so `--apply`
previews remain read-only. Execute-mode stack push, integration rebuild, and
integration push retain manifest-write classification for control-state persistence.

## Replay modes

Rebuilds pick their execution mode automatically; you only override it deliberately.

| Mode | What it does | When you would ask for it |
|---|---|---|
| `plumbing` | replays through Git plumbing, no working tree at all | fastest; chosen automatically on Git ≥ 2.38 |
| `ephemeral` | temporary worktree, removed before the command returns | automatic fallback on older Git |
| `in-place` | replays in the checkout you are already standing in | automatic when the target is the current branch |
| `desk` | persistent worktree, left behind on purpose | resolving a conflict, or validating a non-empty materialized stack when integration cannot safely run it |

```bash
syncwheel stack rebuild <id> --replay-mode desk   # keep the worktree, e.g. to resolve a conflict
syncwheel replay-mode                             # show the repo-local default
syncwheel replay-mode set desk                    # persist it for this clone
```

Selection is four-tier, most specific first: the `--replay-mode` flag, `replay_mode` in the repo-local
`.syncwheel/profile.local.json`, `defaults.replay_mode` in the manifest, then `auto`. An unavailable
mode falls back rather than failing.

**A plumbing conflict never descends silently.** It names the conflicted paths, leaves the filesystem
untouched, and stops with the exact `--replay-mode desk` retry command. Take that escalation: `stack
absorb` and `stack resolve-integration` both need a checkout to resolve in, and plumbing never made
one.

## Lossless checkout repair

Before switching, rebuilding, resetting, relocating, or reaping a dirty or
misplaced checkout, follow [the lossless repair
protocol](references/lossless-repair.md).

> ⚠️ **Rebuilds can silently revert already-applied work.** A `stack rebuild` /
> `int rebuild` reconstructs the branch from the **manifest's commit projection,
> not from the branch's current remote tip**. If the manifest points at a
> pre-cleanup commit (or a range that misses a later fix), the rebuild force-pushes
> the branch back to that older state and the cleanup/fix **disappears** — a real
> regression mode (observed in practice: a cleaned-up file came back after a rebuild
> off a stale projection). **Always:** before rebuilding, update the manifest with
> `syncwheel stack set <id> <rev-or-range>` so the projection includes the latest commit;
> and after every rebuild/sync/publish, diff the rebuilt branch against the expected
> post-fix state to confirm earlier cleanups did not regress.
>
> Replay is now reproducible — the same declared commits on the same base rebuild to the same SHAs, so
> a rebuild that changes nothing is a genuine no-op. That removes the noise, not the hazard: a stale
> projection still force-pushes the branch back to what the manifest says.

## Authoring a new PR stack

```bash
# 1. Ensure a manifest exists (see the tracking decision below)
syncwheel init                                  # shared manifest (.syncwheel/manifest.json)
# 2. Persist the repo tracking policy before branch/push/PR work
syncwheel repo tracking status
syncwheel repo tracking set git-tracked --apply # or local-only
# 3. Declare the stack
syncwheel stack create feature-a --branch pr/feature-a --base origin/main
# 4. Author in a governed lane, never on the shared integration checkout
# syncwheel worktree open feature-a --into feature-a
#    ... make and commit your changes ...
# 5. Record the commits into the manifest, then validate and push
syncwheel stack set feature-a origin/main..HEAD
syncwheel validate && syncwheel plan --json
syncwheel stack push feature-a
```

## When you do not know which PR owns the change yet

Create a draft, then author in a governed lane. A draft owns its commits but is forbidden from
becoming a pull request until you promote it, so the work is tracked from the first commit instead of
sitting unowned until the next rebuild drops it.

```bash
syncwheel stack create --draft caching-experiment       # owned, not proposed
syncwheel worktree open caching-experiment --into caching-experiment
#    ... commit in that lane, then own it through the stack flow ...
syncwheel stack promote caching-experiment --branch pr/caching   # now it is a real PR branch
```

A draft refuses `stack push` to the target remote and names its state as the reason. Under
active-active coordination its *source* ref does publish to the coordination remote, so another clone
can rebuild it from the manifest alone. `syncwheel stack demote <id>` goes back, and refuses when the
stack already has an open PR recorded.

If `plan` reports integration commits that belong to no stack, that is exactly this situation: it now
names `capture-integration` into a new draft as the remedy.

New manifests require every declared stack to participate in integration. Migrate an older manifest
only after classifying absorbed or abandoned stacks:

```bash
syncwheel manifest require-integration
syncwheel manifest require-integration --apply
```

## Delivery lifecycle

Feature stacks are delivered through PRs into the intended delivery branch for
that repo or fork. That target may be `main`, an upstream/default branch, or a
release branch, but it must not be `main-integration`.

`main-integration` is a coordination branch for assembling and testing stacks
before delivery. Do not treat it as a PR target or deployment branch.

A channel branch may be an input to CI/CD, but it remains a pinned Git
composition. `channel publish` proves only the exact published revision and its
receipt. Environment rollout and health require separate deployer evidence.

After the PR merges, fetch the target and prove absorption against the
candidate's own HEAD: check ancestry and require `git cherry <delivery_ref>
<branch>` to contain no `+` patches for squash/rebase merges. A raw tree diff is
not proof when the target has unrelated commits. Align or rebuild integration
from the updated base, then close and reap the stack below.

## Housekeeping: when and how to clean up

Worktrees, PR branches, and `backup/*` branches do not reap themselves — close the
loop or they accumulate. Clean up when any of these holds:

- a stack's PR has merged
- `syncwheel status` shows a worktree or local branch not backing an **active**
  manifest stack (orphans — common after an integration-scheme change)
- `backup/*` branches have piled up
- before handing off a managed repo: leave only the integration checkout, plus any
  `desk` worktree someone is genuinely working in

Rebuilds no longer add to this pile — a worktree now exists only because someone asked for one, so an
unexplained worktree is a real signal rather than routine residue.

Procedure (never destroy unmerged or uncommitted work):

```bash
git fetch --all --prune
git merge-base --is-ancestor <branch> <delivery_ref>
git cherry <delivery_ref> <branch>           # if ancestry failed, this must contain no "+" rows
syncwheel stack close <id> -R merged --force # close metadata before removing its worktree
git worktree remove <worktree-path>          # non-force; retain and report dirty/conflicted/submodule-blocked trees
git branch -d <branch>                       # use -D only after squash/rebase absorption was proved above
git worktree prune --dry-run
git worktree prune
```

For an active-active manifest, prefer its tombstone-aware lifecycle instead:

```bash
syncwheel stack close <id> -R merged --force
syncwheel gc
syncwheel gc --apply
```

`stack close` never deletes a remote branch. Automatic cleanup after `sync` or
`publish`, and explicit `gc --apply`, only remove old tombstoned local artifacts
that are clean, unlocked, non-current, and recoverable from the remote state.

Retain recovery refs and stashes until every dependent lane is delivered and
independently verified; only then prune obsolete backups. Never delete unique
unmerged commits or force-remove a dirty, conflicted, or submodule-blocked
worktree — retain and report it unless that exact destructive scope is approved.

## Decision: Syncwheel tracking policy

This is a repo-local Syncwheel policy, not a social guess. Before editing in,
branching, pushing, opening a PR for, recovering, or handing off a shared or
Syncwheel-managed Git repo, run:

```bash
syncwheel repo tracking status
```

If `syncwheel_tracking` is missing, stop before branch, push, PR, stack,
worktree, recovery work, or final handoff. Do not guess, do not default
silently, and do not continue with a provisional policy. Ask the maintainer/user
whether this repo should be `git-tracked` or `local-only`, then persist the
answer:

```bash
syncwheel repo tracking set git-tracked --apply
syncwheel repo tracking set local-only --apply
```

Then read how far you may take a change without asking:

```bash
syncwheel repo authority status
```

`ai-managed` means the repository's configured Syncwheel pipeline runs
unattended. The manifest defines the pipeline (stacks, PRs, landing, channels,
or journal publish); `authority` only says whether you may run it without a
human at each stage. `source_change` covers source delivery up to and including
merge or `journal publish`; `runtime_change` covers the rollout that pipeline
leads to (deployment channels, install, restart) and must be listed on its own.
`destructive_rewrite`, external sends, money, and work outside the named scope
stay gated whatever the block says. Missing block means `human-gated`. Never
set or widen this policy yourself.

### `git-tracked` → commit the manifest

Commit `.syncwheel/manifest.json` (and `.syncwheel/manifests/README.md`). Keep
personal overlays (`*.local.json`, `profile.local.json`) gitignored.

Benefits:
- the stack/integration topology is **versioned and shared** — every agent that
  clones inherits the same deterministic plan
- reproducible across machines without out-of-band setup
- the manifest is the team's **coordination contract**

Use this when the repo wants Syncwheel itself tracked under Git. Syncwheel writes
a managed `.gitignore` block for local-only metadata and repo-local worktrees
under `.syncwheel/wt/`.

New `git-tracked` manifests with a configured publication remote initialize
active-active coordination by default. Existing manifests stay legacy until the
maintainer explicitly opts in with:

```bash
syncwheel coordination init --remote origin --apply
```

### `local-only` → keep Syncwheel untracked

Exclude `.syncwheel/` via `.git/info/exclude` (local, does
not touch the committed `.gitignore`).

Benefits:
- you still get stacks, deterministic reconcile, and the ledger
- you do **not** impose Syncwheel config on a maintainer who may not use it
- your PRs stay clean — only the real change is proposed
- coordination/recovery happens via the canonical remote + `resume`

`local-only` does not automatically create shared coordination state. It may
opt in only with the explicit `coordination init --remote ... --apply` command.

Use `syncwheel repo tracking set ... --apply` to migrate between modes. The CLI
edits only Syncwheel-managed ignore blocks; if manual `.gitignore` entries would
hide `.syncwheel/manifest.json`, stop and ask for a repository decision.

## Handoff checklist

Before final response after touching a Git repo:

- run `git status --short --branch` and state whether the worktree is clean,
  intentionally dirty, or blocked
- run the repo's required validation, or name the exact validation not run and why
- for `git-tracked` repos, commit and push the scoped work unless the user or
  repository policy explicitly requires local-only delivery
- treat Syncwheel manifest changes from stack create/set/close or integration bookkeeping as
  scoped work: commit and push them before handoff, or explicitly report the approved reason they remain local
- if the user asked to synchronize a repo with outstanding PRs/upstreams,
  fetch/prune, inspect each PR's own HEAD, then check ancestry and `git cherry`
  patch absorption. If you integrate PR heads directly into the target and push,
  re-query the forge; it may then mark those PRs merged.
- when applying stashed local work after PR synchronization, expect fixture-only conflicts. Resolve by preserving the synchronized upstream behavior and reapplying only the scoped new feature/logging changes; then rerun tests before committing.
- if the user requested local-only edits, list the dirty files and the next
  command/decision needed to finish delivery
- never treat unrelated dirty files as a reason to skip this checklist; isolate
  your own changes and report unrelated residue separately

> **Manifest self-reference rule:** treat manifest edits and Syncwheel-version
> bumps as control-plane metadata, not as normal stack-owned product commits.
> Keep them in an admin checkout or a dedicated maintenance PR that is excluded
> from `integration.stacks`.

## Multi-agent / multi-machine

A shared, committed manifest plus the append-only ledger is what lets many agents
coordinate deterministically. On a fresh machine or a new agent, recover shared
state with `syncwheel resume` instead of improvising branch ownership.

## GitHub PR merge policy

For an explicitly `ai-managed` repository whose authority allows
`source_change`, a maintainer may opt a clone into the private GitHub merge
path:

```bash
syncwheel repo pr-merge-policy status --json
syncwheel repo pr-merge-policy set github --repository OWNER/REPO --base main \
  --method squash --allow-bypass required_reviews --merge-actor LOGIN \
  --pr-author LOGIN --commit-author LOGIN --head-repository OWNER/REPO
```

`set` and `clear` are dry-run until `--apply`; `profile.local.json` must be
ignored and untracked. At least one provenance filter is required and all
configured filters pass together. The shared manifest never carries this
private policy.

Plan first, then apply with the exact values from the plan:

```bash
syncwheel stack merge-pr STACK --json
syncwheel stack merge-pr STACK --operation-id ID --plan-digest DIGEST --apply
```

The fixed `syncwheel-github` adapter is the only component that calls `gh`.
It observes the exact PR, actor permissions, commit identities, repository
rules, review threads, and checks. The core fails closed on drift, conflicts,
CI states other than `SUCCESS`/`SKIPPED`, changes requested, unresolved
threads, merge queues, unknown rules, or an unproven review-only block. It
uses `--admin` only for a proven required-review bypass and always includes
`--match-head-commit`; it never deletes the remote branch. An interrupted
operation is reconciled by observation with the same operation id and digest,
never by automatically retrying the merge.

## More

See `docs/deployment-channels.md` for the channel lifecycle,
`docs/github-pr-merge.md` for the GitHub PR merge policy,
`docs/manifest-tracking.md` for the full tracking policy, `docs/ai-agents.md`
and `docs/agent-procedure.md` for the agent contract, and `docs/core-procedure.md`
for the canonical recovery procedure. An automated post-merge cleanup path is
specified in `docs/design/housekeeping-after-merge.md`.
