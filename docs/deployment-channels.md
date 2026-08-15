# Deployment channels

A Syncwheel channel is a **pinned, ordered, rebuildable Git branch composition**.
It selects exact revisions of existing stacks and materializes them on a
dedicated branch. This makes a tested feature set repeatable without changing
the stacks themselves.

The word deployment describes the channel's intended use, not an action that
Syncwheel performs. Publishing a channel branch does **not** deploy an
application or prove that any environment is running it. A CI/CD system may
observe the published branch and deploy it under its own policy.

## Keep the four objects separate

| Object | Purpose | Changes when a stack moves? |
|---|---|---|
| Stack | One owned change stream and its PR branch | Yes |
| Integration branch | Current combined projection of the manifest's integration stacks | Yes |
| Channel | Selected stacks pinned to exact revisions in a declared order | Only after an explicit channel refresh or edit |
| Deployment | External environment state managed by CI/CD or another system | Outside Syncwheel |

A channel is not a second integration branch. Integration represents the full
current working composition declared by `integration.stacks`; a channel can
select a subset, change its order, and keep old pins while development
continues.

## Manifest contract

Channels are available only for `repository_mode: "delivery"` and require
manifest version 3. Older clients fail closed on a version 3 manifest rather
than silently ignoring channel refs during coordinated publication.

The following excerpt sits alongside the existing defaults, integration,
stacks, and required coordination block:

```json
{
  "version": 3,
  "channels": [
    {
      "id": "test",
      "branch": "channel/test",
      "lifecycle": "shared",
      "base": "origin/main",
      "baseRevision": "9999999999999999999999999999999999999999",
      "remote": "origin",
      "composition": [
        {
          "stack": "api",
          "branch": "pr/api",
          "stackBase": "origin/main",
          "stackBaseRevision": "9999999999999999999999999999999999999999",
          "branchRevision": "1111111111111111111111111111111111111111",
          "dependsOn": [],
          "commits": [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
          ]
        }
      ]
    }
  ]
}
```

Each composition entry records all of the evidence required to rebuild the
same branch later:

- the symbolic base plus its pinned full revision;
- the stack id and branch name, plus the stack's symbolic and pinned base;
- the full stack branch revision observed when the entry was pinned;
- the exact ordered list of full commit ids to replay;
- the stack's exact `depends_on` declaration as `dependsOn` provenance.

Every declared dependency must be present earlier in the same channel. Missing,
late, duplicate, or self dependencies fail closed. Pinning also requires the
stack's declared commit list to equal its complete pinned branch range exactly.

Explicit stack `depends_on` declarations are a manifest version 3 contract.
Version 1 and 2 manifests keep their historical base-chain behavior without
publishing dependency metadata through schema 2. During the reviewed version
2-to-3 channel migration, Syncwheel derives the direct dependency of a stack
whose base is another declared stack branch and writes the explicit v3 field.

Short ids and moving refs are not accepted as pins. A channel never follows its
symbolic base or a stack implicitly.

### Shared and ephemeral channels

- `shared` channels have no expiry. Close them explicitly when they are no
  longer part of the team's deployment flow.
- `ephemeral` channels carry `createdAt` and `expiresAt` ISO-8601 timestamps.
  Expiry makes stale state visible; it does not silently delete local or remote
  branches. Cleanup remains explicit with `channel close`.

## Plan, apply, and receipts

Every channel mutation is preview-first and digest-bound. `create`, `add`,
`remove`, `replace`, `refresh`, `promote`, `resolve`, `apply`, `publish`, and
`close` first emit a `channelPlan`; mutation requires the exact plan digest plus
`--apply`:

1. A preview observes the relevant manifest and operation inputs. For apply and
   publish, `channel plan` reports the pinned `baseRevision`, symbolic base tip
   as `currentBaseRevision`, `baseDrifted`, stack pins, local branch, and remote
   ref. Drift never changes the pin implicitly.
2. The plan records an observation revision and a digest over the canonical
   plan. Its optional `operationId` is deliberately excluded from `planDigest`.
3. The matching mutation accepts only that exact digest. If the manifest, stack
   pin, base, local branch, or observed remote state changed, it stops as stale.
4. A successful channel apply writes a receipt containing the plan digest and
   resulting branch revision. Stale-plan and replay failures before the atomic
   ref update leave the previous channel ref intact.
5. `channel publish` publishes the applied revision with an exact lease and
   records the published revision in its receipt.

Each applied channel mutation writes durable ledger events in order: `started`,
`prepared`, then a terminal `receipt`. Receipts bind `operationId`,
`planDigest`, and `observationRevision` and use one of five terminal statuses:
`succeeded`, `failed`, `partial`, `unknown`, or `cancelled`. Apply and publish
also bind `compositionDigest` to the resulting `tip`; publication records
`publishedRevision` and, when coordinated, `coordinationState`.
`deploymentAsserted` is always false.

Plans give every authoritative action a stable id, target, before state, and
intended after state. Terminal receipts preserve that envelope and report a
separate outcome for each action, so a partial or unknown result identifies
exactly which manifest, local-ref, remote-ref, or coordination-state step is in
question. Receipt timestamps cover the complete started-to-completed interval.

Every writer of an existing delivery manifest shares one per-manifest lock and
compare-before-write check. A stale writer stops instead of replacing newer
channel or stack state. The complete JSON document is written to a same-folder
temporary file, flushed, and atomically replaced; a durability failure after
replacement is reported as unknown and must be reconciled, never blindly
retried.

For active-active manifests, publication moves the channel ref and coordination
state atomically. A lease loss, post-plan remote change, unsupported atomic
push, conflict, or unknown required observation is a hard stop. Re-plan from
the new observation; do not retry with a raw or force push.

## Channel-local resolution

If the pinned composition conflicts, resolve it without rewriting a source
stack or the integration branch. Create one full-SHA snapshot commit whose only
parent is the channel's pinned `baseRevision`, then preview and apply `channel
resolve --revision`.

The manifest stores:

- `forPinDigest`: digest of the raw pinned base and stack composition;
- `revision`: the full resolution commit;
- `tree`: the commit's exact tree;
- `parentRevision`: the pinned channel base.

`pinDigest` describes only raw pins; `compositionDigest` also includes the
optional resolution. Materialization uses the resolution revision only while
`forPinDigest` matches. Add, remove, replace, and refresh invalidate it;
promotion copies a still-valid source resolution with the exact pins. Use
`channel resolve --clear` to remove it deliberately.

Without a resolution, materialization uses Git plumbing on Git 2.38 or newer.
Older Git falls back to a self-removing temporary worktree; routine channel
apply never leaves a persistent worktree behind.

## Lifecycle

The normal flow is:

```text
create -> compose -> plan -> apply -> verify -> publish
             ^          |
             |          v
       add/remove/   plan-bound receipt
       replace/refresh
```

Use `channel diff` before planning to review pin drift against current stack
branches, or to compare two declared channel compositions. Promotion copies the
source's symbolic base, pinned base revision, and exact composition to the
target; it neither merges channel branch history nor deploys an environment.
Close obsolete shared and ephemeral channels explicitly after confirming that
their branch is no longer an external deployment input.

## CLI workflow

Inspect the versioned machine contract and current state first:

```bash
syncwheel channel contract
syncwheel channel list --json
syncwheel channel show dev
syncwheel channel diff dev
syncwheel channel diff dev --other test
```

The helper below demonstrates the required preview/apply handshake for manifest
mutations. It passes a stable operation id, extracts the preview's exact digest,
then repeats the same command with `--plan-digest` and `--apply`:

```bash
channel_apply_preview() {
  channel_operation_id="$1"
  shift
  channel_preview="$(syncwheel channel "$@" \
    --operation-id "$channel_operation_id")" || return
  channel_plan_digest="$(printf '%s' "$channel_preview" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')" || return
  syncwheel channel "$@" --operation-id "$channel_operation_id" \
    --plan-digest "$channel_plan_digest" --apply
}
```

`--operation-id` is optional. When omitted, Syncwheel derives a deterministic
id from the plan; when supplied, pass the same id to preview and apply. It is
excluded from `planDigest`, but one id can never be rebound to another plan.

Use it with the real mutation commands:

```bash
channel_apply_preview create-dev-001 create dev --lifecycle shared \
  --stack api --stack web
channel_apply_preview create-preview-184 create feature-184 \
  --lifecycle ephemeral --expires-at 2026-09-01T12:00:00Z --stack api

channel_apply_preview add-worker-001 add dev worker --position 1
channel_apply_preview remove-web-001 remove dev web
channel_apply_preview replace-worker-001 replace dev worker worker-v2
channel_apply_preview refresh-api-001 refresh dev --stack api
channel_apply_preview refresh-all-001 refresh dev
channel_apply_preview promote-preview-001 promote feature-184 test
```

`refresh` always re-pins the channel base. With one or more `--stack` options it
re-pins only those stack entries; without them it re-pins every entry. Add,
remove, replace, and refresh clear a channel resolution. Promotion copies the
source base, base revision, composition, and valid resolution into an existing
target while retaining the target's id, branch, remote, lifecycle, and expiry.

The first applied create migrates an eligible coordinated version 2 manifest to
version 3. A version 1 manifest must first opt into coordination with
`syncwheel coordination init --remote <remote> --apply`; create never performs
that policy change implicitly.

Creation claims a new channel ref; it is not an adoption command. The selected
branch must be absent both locally and on its remote, and it cannot overlap a
base, integration branch, stack source/target, or coordination-state ref. If a
previously closed channel left its remote branch behind, choose a new branch
name. Syncwheel 0.33 deliberately has no implicit `--adopt` path.

Resolve or clear a channel-local snapshot through the same handshake:

```bash
channel_apply_preview resolve-dev-001 resolve dev \
  --revision 1111111111111111111111111111111111111111
channel_apply_preview clear-resolution-001 resolve dev --clear
```

Materialization and publication each require their own fresh plan. `channel
plan` emits JSON by default:

```bash
channel_apply_plan="$(syncwheel channel plan dev --operation apply \
  --operation-id apply-dev-001)"
channel_apply_digest="$(printf '%s' "$channel_apply_plan" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')"
syncwheel channel apply dev --operation-id apply-dev-001 \
  --plan-digest "$channel_apply_digest" --apply

channel_publish_plan="$(syncwheel channel plan dev --operation publish \
  --operation-id publish-dev-001)"
channel_publish_digest="$(printf '%s' "$channel_publish_plan" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')"
syncwheel channel publish dev --operation-id publish-dev-001 \
  --plan-digest "$channel_publish_digest" --apply
```

The publish plan must be created after materialization because the local channel
revision is part of its observation. A `shared` channel refuses publication
while its composition contains draft stacks; an `ephemeral` channel may publish
draft stacks for temporary testing. With active-active coordination, the
channel remote must equal the coordination remote.

Close is also digest-bound. It never deletes the remote branch and records an
active-active tombstone. `--delete-local` is allowed only with matching apply
evidence and when the local ref is not checked out:

```bash
channel_apply_preview close-preview-184 close feature-184 \
  --reason expired --delete-local
```

Inspect durable operations and receipts without changing state:

```bash
syncwheel channel operation list
syncwheel channel operation list --channel dev --status unknown
syncwheel channel operation show publish-dev-001
syncwheel channel receipt show dev
```

The list view also accepts the non-terminal filters `pending` and `prepared`.
An interruption before the authoritative mutation boundary records `cancelled`
and changes nothing. At or after that boundary, interruption records `unknown`;
the same operation id is never used to replay the mutation.

`channel receipt show [channel]` includes terminal operation receipts plus the
compatibility apply, publish, and close receipts from the earlier channel
ledger surface.

If an outcome is unknown, reconciliation only observes refs/manifest state. It
never repeats the original mutation. Applying its exact plan appends terminal
evidence:

```bash
channel_reconcile_plan="$(syncwheel channel operation reconcile publish-dev-001)"
channel_reconcile_digest="$(printf '%s' "$channel_reconcile_plan" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')"
syncwheel channel operation reconcile publish-dev-001 \
  --plan-digest "$channel_reconcile_digest" --apply
```

`channel reconcile-outcome` remains an alias for `channel operation reconcile`.

## Failure behavior

- **Stale plan:** no ref or manifest mutation; observe again and create a new
  plan.
- **Replay conflict:** no partial channel ref update; resolve the underlying
  stack/base conflict or change the composition, then re-plan.
- **Post-plan remote change or lease loss:** publication stops without updating
  the remote; inspect the new state and create a fresh publish plan.
- **Unknown or unreadable state:** the plan records it and any operation that
  requires the missing observation refuses to proceed. Do not guess a revision.
- **Unknown outcome after a ref or remote operation:** do not retry blindly;
  inspect the operation and use `channel operation reconcile` to append observed
  terminal evidence. Create a new operation only after the outcome is
  understood.
- **Cancelled operation:** cancellation before the authoritative boundary is a
  terminal no-op. An interruption after that boundary is `unknown`, not safely
  cancelled, and requires reconciliation.
- **Expired ephemeral channel:** the manifest timestamp remains visible until
  explicit close; do not assume its external deployment has disappeared.

## CI/CD boundary

Syncwheel's terminal proof is the local or published channel Git revision plus
its receipt. Environment deployment requires separate evidence from the
external deployer, such as a deployment id, artifact digest, environment URL,
and health check. Never report an environment as deployed from channel branch
publication alone.
