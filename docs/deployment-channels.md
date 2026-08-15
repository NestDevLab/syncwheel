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
          "branchRevision": "1111111111111111111111111111111111111111",
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
- the stack id and branch name;
- the full stack branch revision observed when the entry was pinned;
- the exact ordered list of full commit ids to replay.

Short ids and moving refs are not accepted as pins. A channel never follows its
symbolic base or a stack implicitly.

### Shared and ephemeral channels

- `shared` channels have no expiry. Close them explicitly when they are no
  longer part of the team's deployment flow.
- `ephemeral` channels carry `createdAt` and `expiresAt` ISO-8601 timestamps.
  Expiry makes stale state visible; it does not silently delete local or remote
  branches. Cleanup remains explicit with `channel close`.

## Plan, apply, and receipts

Manifest composition changes are preview-first and require `--apply` to save.
Channel branch materialization and publication are additionally plan-bound:

1. `channel plan` observes the relevant manifest, pinned and current symbolic
   base revisions, stack pins, local branch, and remote ref. `baseDrifted`
   exposes a moved symbolic base without silently changing the pin.
2. The plan records an observation revision and a digest over the canonical
   plan.
3. `channel apply` accepts only the digest of that exact plan. If the manifest,
   stack pin, base, local branch, or observed remote state changed, it stops as
   stale.
4. A successful apply writes a receipt containing the plan digest and resulting
   branch revision. Stale-plan and replay failures before the atomic ref update
   leave the previous channel ref intact.
5. `channel publish` publishes the applied revision with an exact lease and
   records the published revision in its receipt.

Apply and publish print the receipt as JSON and append it to the Syncwheel
ledger. The receipt binds `planDigest`, `observationRevision`, and
`compositionDigest` to the resulting `tip`; publication also records
`publishedRevision` and, when coordinated, `coordinationState`.
`deploymentAsserted` is always false.

For active-active manifests, publication moves the channel ref and coordination
state atomically. A lease loss, post-plan remote change, unsupported atomic
push, conflict, or unknown required observation is a hard stop. Re-plan from
the new observation; do not retry with a raw or force push.

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

The `channel` commands expose listing, inspection, composition edits,
plan-bound materialization, leased publication, and explicit cleanup. Run
`syncwheel channel <command> --help` before mutation when scripting against a
specific installed release.

Start read-only:

```bash
syncwheel channel list --json
syncwheel channel show dev
```

Create previews the manifest change. Repeat it with `--apply` to create the
channel and, for a coordinated version 2 manifest, perform the explicit version
3 migration:

```bash
syncwheel channel create dev --lifecycle shared \
  --stack api --stack web
syncwheel channel create dev --lifecycle shared \
  --stack api --stack web --apply

syncwheel channel create feature-184 --lifecycle ephemeral \
  --expires-at 2026-09-01T12:00:00Z --stack api --apply
```

A legacy version 1 manifest must first opt into coordination with
`syncwheel coordination init --remote <remote> --apply`; channel creation never
performs that policy change implicitly.

Composition edits are also previews unless `--apply` is present:

```bash
syncwheel channel add dev worker --position 1
syncwheel channel add dev worker --position 1 --apply
syncwheel channel remove dev web --apply
syncwheel channel replace dev api api-v2 --apply
syncwheel channel refresh dev --stack worker --apply
syncwheel channel refresh dev --apply
```

`refresh` always re-pins the channel base. With one or more `--stack` options it
re-pins only those stack entries; without them it re-pins every entry. Inspect
pin drift or compare two declared compositions:

```bash
syncwheel channel diff dev
syncwheel channel diff dev --other test
```

Promotion copies the source channel's symbolic base, exact pinned base revision,
and exact composition into an existing target channel. The target retains its
own id, branch, remote, lifecycle, and expiry. Preview it first:

```bash
syncwheel channel promote feature-184 test
syncwheel channel promote feature-184 test --apply
```

Materialization and publication each require their own fresh plan digest:

```bash
channel_apply_digest="$(syncwheel channel plan dev |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')"
syncwheel channel apply dev --plan-digest "$channel_apply_digest" --apply

channel_publish_digest="$(syncwheel channel plan dev --operation publish |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["planDigest"])')"
syncwheel channel publish dev --plan-digest "$channel_publish_digest" --apply
```

Running `channel apply` or `channel publish` without `--apply` prints its plan.
The publish plan must be created after materialization because the local channel
revision is part of its observation.

A `shared` channel refuses publication while its composition contains draft
stacks. An `ephemeral` channel may publish draft stacks for temporary testing.
With active-active coordination, the channel remote must equal the coordination
remote so the ref and coordination state can move atomically.

The close command previews by default, never deletes the remote branch, and
records a tombstone in active-active coordination. Local deletion is optional
and allowed only when the current local ref has matching apply evidence and is
not checked out:

```bash
syncwheel channel close feature-184 --reason expired
syncwheel channel close feature-184 --reason expired --delete-local --apply
```

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
  compare the actual local/remote ref with the latest ledger receipt and create
  a new plan only after the outcome is understood.
- **Expired ephemeral channel:** the manifest timestamp remains visible until
  explicit close; do not assume its external deployment has disappeared.

## CI/CD boundary

Syncwheel's terminal proof is the local or published channel Git revision plus
its receipt. Environment deployment requires separate evidence from the
external deployer, such as a deployment id, artifact digest, environment URL,
and health check. Never report an environment as deployed from channel branch
publication alone.
