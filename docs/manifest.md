# Manifest

The preferred source of truth is `.syncwheel/manifest.json`.

## Shape

```json
{
  "version": 1,
  "syncwheel_tracking": "git-tracked",
  "syncwheel_worktree_root": ".syncwheel/wt",
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "fork",
    "base_branch": "main",
    "base_ref": "origin/main",
    "integration_membership": "required",
    "replay_mode": "auto"
  },
  "integration": {
    "branch": "integration/project-stack",
    "base": "origin/main",
    "strategy": "merge-stacks",
    "stacks": ["feature-a", "feature-b"]
  },
  "landing": {
    "mode": "direct",
    "strategy": "merge",
    "checks": {
      "id": "landing-quality",
      "all": [
        {"id": "unit", "local": {"scope": "stack", "argv": ["make", "test"]}}
      ]
    }
  },
  "stacks": [
    {
      "id": "feature-a",
      "branch": "pr/feature-a",
      "state": "published",
      "base": "origin/main",
      "target_remote": "origin",
      "target_branch": "main",
      "integration_branch": "integration/project-stack",
      "publication": { "enabled": true },
      "commits": ["abc1234", "def5678"]
    }
  ]
}
```

## Journal mode

Omitting `repository_mode` preserves the delivery manifest behavior above.
Journal repositories use this separate contract:

```json
{
  "version": 1,
  "repository_mode": "journal",
  "syncwheel_tracking": "git-tracked",
  "journal": {
    "branch": "journal",
    "remote": "origin",
    "include": ["**"],
    "exclude": [".cache/**", "tmp/**"],
    "max_file_bytes": 10485760,
    "interval": "30m"
  }
}
```

`branch`, `remote`, the explicit include/exclude lists, and maximum size are
required. Only the interval defaults, to `30m`. Journal mode forbids
delivery stack, integration, reconcile, sync, and publish commands; use the
`journal` command family instead.

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

### Version 2 active-active manifests

The sample above is a valid legacy version 1 manifest. Version 2 adds a required
`coordination` block for durable, multi-device publication:

```json
{
  "version": 2,
  "syncwheel_tracking": "git-tracked",
  "defaults": {
    "canonical_remote": "origin",
    "publication_remote": "origin",
    "base_branch": "main",
    "base_ref": "origin/main"
  },
  "coordination": {
    "mode": "active-active",
    "id": "default",
    "remote": "origin",
    "state_branch": "syncwheel/state/default",
    "gc": {
      "worktree_grace_days": 7,
      "backup_retention_days": 30,
      "backup_keep": 2
    }
  }
}
```

Create this form for a new shared repository only when the publication remote
is configured:

```bash
syncwheel init --syncwheel-tracking git-tracked --publication-remote origin
```

If the remote is unavailable, initialization stops instead of silently turning
coordination off. Use `--no-coordination` only for an intentional persisted
opt-out. Existing version 1, `local-only`, and disabled manifests opt in
explicitly:

```bash
syncwheel coordination init --remote origin --apply
syncwheel coordination disable --apply
```

`coordination.remote` must equal `defaults.publication_remote`, and its state
branch is always `syncwheel/state/<coordination-id>`. See
[active-active-coordination.md](design/active-active-coordination.md) for the
publication and recovery protocol.

### Version 3 deployment channels

Version 3 adds `channels` to delivery manifests. A channel is an ordered branch
composition pinned to exact stack revisions and commit lists; it is not an
environment deployment. Clients that understand only manifest version 2,
including Syncwheel 0.32, fail closed on version 3 instead of publishing
coordination state without its channel refs.

The excerpt below is added alongside the version 2 defaults, integration,
stacks, and coordination fields shown above:

```json
{
  "version": 3,
  "channels": [
    {
      "id": "dev",
      "branch": "channel/dev",
      "lifecycle": "shared",
      "base": "origin/main",
      "baseRevision": "9999999999999999999999999999999999999999",
      "remote": "origin",
      "composition": [
        {
          "stack": "feature-a",
          "branch": "pr/feature-a",
          "stackBase": "origin/main",
          "stackBaseRevision": "9999999999999999999999999999999999999999",
          "branchRevision": "1111111111111111111111111111111111111111",
          "commits": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
          "dependsOn": []
        }
      ]
    }
  ]
}
```

The first exact-digest application of a `channel create` preview migrates an
eligible version 2 delivery manifest to version 3. Review and commit that
manifest change with the channel change. Journal mode rejects channels.
Explicit stack `depends_on` metadata is also version 3-only. Existing version 1
and 2 stack base chains remain readable; migration derives direct dependencies
from bases owned by other declared stacks before the v3 manifest is saved.

`shared` channels have no expiry. `ephemeral` channels require ISO-8601
`expiry.createdAt` and `expiry.expiresAt`; expiry is observable metadata, not an
automatic deletion. Both lifecycles use explicit `channel close` cleanup.

Every channel mutation previews a `channelPlan`. Applying requires its exact
`planDigest`; an optional `operationId` supplies durable idempotency but is
excluded from the digest. Applied work records `started`, `prepared`, and a
terminal receipt (`succeeded`, `failed`, `partial`, `unknown`, or `cancelled`).
`channel operation reconcile` only observes an uncertain outcome and appends a
digest-bound receipt; it never retries the original mutation.

Each composition entry must preserve the stack's complete `depends_on` closure
in order, and its commit list must equal the exact pinned
`stackBaseRevision..branchRevision` range. An optional channel `resolution`
stores `forPinDigest`, `revision`, `tree`, and `parentRevision`; edits invalidate
it, while promotion copies a still-valid resolution with the source pins.

See [deployment-channels.md](deployment-channels.md) for the CLI and safety
contract.

Create a personal local manifest:

```bash
python3 scripts/syncwheel.py init --personal alice
```

This writes `.syncwheel/manifests/alice.local.json` and sets the integration
branch to `integration/alice/main`. Syncwheel keeps that manifest's ledger in
`.syncwheel/manifests/alice.local-ledger/`, isolated from the shared manifest
and from other personal manifests.

Use the personal manifest with the short `--personal` flag:

```bash
python3 scripts/syncwheel.py check -p alice
```

For automation, use `check --strict`: it exits non-zero when validation emits
errors or warnings, when a stack branch has commits outside its declared source
projection locally or on the publication remote, when a published local/remote
stack pair is not aligned, or when the deterministic plan is non-empty. Plain
`check` remains an observational command and exits non-zero only for validation
errors.

Or set the personal manifest as the default for the current clone:

```bash
python3 scripts/syncwheel.py use alice
python3 scripts/syncwheel.py check
python3 scripts/syncwheel.py use --shared
```

Create stack entries through the CLI:

```bash
python3 scripts/syncwheel.py stack create feature-a --branch pr/feature-a
python3 scripts/syncwheel.py stack set feature-a origin/main..HEAD
```

Create a private-but-owned draft with its required source branch already
materialized. Drafts still participate in `integration.stacks`; only
publication is disabled.

```bash
python3 scripts/syncwheel.py stack create exploration --draft
# branch: syncwheel/draft/exploration
python3 scripts/syncwheel.py stack promote exploration
# branch: pr/exploration, state: published
```

`stack promote` accepts `--branch <pr-branch>` when the published branch does
not use the default `pr/<stack-id>` name. It renames the local branch and sets
`state: published` with `publication.enabled: true`. `stack demote <stack>` is
the reverse state transition but intentionally does not rename the branch; it
refuses a stack with a populated `github.pr` value. Rebuilds remain allowed, so
a draft remains recoverable from the manifest.

A draft's source ref and its PR branch are two independent publication levels.
Under active-active coordination, `stack push` and `reconcile --push` publish
`refs/heads/syncwheel/draft/<stack>` to the coordination remote through the
normal atomic leased publication, which is what lets another clone rebuild the
draft from the manifest alone. Every other destination — the stack's
`target_remote`, an overridden `--remote`, or any manifest without active
coordination — is refused and names the `draft` state.

For active-active manifests, promotion atomically publishes the replacement
branch and coordination state. The former draft remote branch is retained as a
tombstoned recovery ref; Syncwheel does not delete it. If a reconcile-created
`.syncwheel/wt/<draft-branch>` directory exists, promotion prints its retained
path rather than moving it silently.

New manifests require every declared stack to participate in integration. Migrate
an existing legacy manifest only after closing stacks that are already absorbed
or abandoned:

```bash
python3 scripts/syncwheel.py manifest require-integration
python3 scripts/syncwheel.py manifest require-integration --apply
```

## Rules

- `version` is `1` for legacy manifests, `2` for manifests with a required
  `coordination` block, or `3` for channel-aware delivery manifests
- `syncwheel_tracking`, when present, must be `git-tracked` or `local-only`
- `syncwheel_worktree_root` defaults to repo-relative `.syncwheel/wt`
- new manifests set `defaults.integration_membership` to `required`; legacy
  manifests without it remain compatible until explicitly migrated
- required membership means every declared stack id must appear in
  `integration.stacks`; use a normal Git worktree for work that is not ready to
  enter Syncwheel's integration lifecycle
- `defaults.replay_mode` is optional and is the repository-wide default replay
  execution mode: `auto`, `plumbing`, `in-place`, `ephemeral`, or `desk`. It is
  the shared default for contributors who should agree on one; a `replay_mode`
  in the repo-local `.syncwheel/profile.local.json` overrides it, and
  `--replay-mode` on the command overrides both. Omitted means `auto`.
  It is deliberately absent from coordination state: it changes how a ref was
  produced, not what it contains, so it is not shared topology.
- `landing` is optional. Its mode is `disabled` (the default) or `direct`; an
  absent or disabled policy needs the per-request `stack land --allow-direct`
  bypass. `strategy` is `merge` (fast-forward first, deterministic merge if
  necessary) or `ff-only`.
- `landing.checks`, when present, is a uniquely identified `all`/`any` tree.
  Leaves are `local` (`scope` `stack` or `integration`, argv, optional timeout),
  `attestation` (same scope plus verifier argv), or `pr` (remote-check route
  marker). There is no negation or ambient-expression language. Attestation
  verifiers receive JSON on stdin and must emit `{valid:true, subjectRevision,
  issuer}` on stdout.
- version 2 or 3 `coordination.mode` is `active-active` or persisted `disabled`
- version 2 or 3 coordination must use the same named remote as
  `defaults.publication_remote`
- version 3 is required when `channels` is non-empty; channels are valid only in
  delivery mode
- every channel id and channel branch must be unique
- channel lifecycle is `shared` or `ephemeral`; only ephemeral channels carry
  expiry metadata
- every channel pins the full `baseRevision` resolved from its symbolic `base`;
  `channel refresh` is the explicit operation that advances this pin
- each channel composition entry pins one unique stack with its branch, full
  stack base provenance, observed branch revision, exact ordered full commit
  list, and exact `dependsOn` declaration
- channel order is significant and independent of `integration.stacks`
- every pinned `dependsOn` stack must appear earlier in the same channel;
  pinning fails unless each stack's declared commit list exactly equals its
  pinned base-to-branch range
- active-active channels must publish to `coordination.remote`; shared channel
  publication refuses draft stacks, while ephemeral channels may contain them
- an optional channel `resolution` contains `forPinDigest`, `revision`, `tree`,
  and `parentRevision`; it is valid only for the exact raw pins, is cleared by
  composition/refresh edits, and is copied by promotion with those pins
- every stack id must be unique
- every stack branch must be unique
- `state` is `draft` or `published` and defaults to `published`; `publication`
  is normalized to `{ "enabled": state != "draft" }`
- every declared commit must exist in Git
- `commits` remain the source projection used to rebuild a stack branch
- `integration_commits`, when present, is the resolved projection used only for
  integration. Record it after a conflict creates new integration commits; it
  prevents Syncwheel from treating those commits as source-branch commits.
- `integration.strategy` is optional and defaults to `cherry-pick`
- supported integration strategies are:
  - `cherry-pick`: replay all declared commits into integration as a linear history
  - `merge-stacks`: merge each declared stack branch into integration in manifest order with `--no-ff`
- every persistent integration change should belong to exactly one declared stack unless it is explicit temporary debug work
- a commit changing only `.syncwheel/manifest.json` is treated as integration
  control-plane metadata, not an unclassified product change

## What validation checks

`syncwheel.py validate` checks:
- manifest structure
- channel version, delivery-mode, lifecycle, expiry, and uniqueness rules
- existence of each channel symbolic base, pinned base revision, configured
  remote, stack-base provenance, full pinned stack branch revision, and exact
  pinned commit range
- channel dependency closure/order and channel-local resolution binding, parent,
  tree, and object availability
- current stack-pin drift and expired ephemeral channels (reported without
  automatic cleanup)
- existence of integration base ref
- existence of PR branches
- existence of declared commits
- whether PR branches contain declared commits
- whether integration contains declared commits
- whether integration references unknown stacks
- whether a required-membership manifest excludes declared stacks from integration
- whether integration contains non-merge commits that are not declared in any stack

Unmapped integration commits are reported as warnings plus a
`classify_integration_commits` plan action. The tool can identify the commits,
but a human or AI agent still needs to decide which stack owns each change.

### Resolved integration projections

When a declared stack commit conflicts during integration, resolve the conflict
on the integration branch and record the resulting commit separately. This keeps
the source branch immutable while allowing deterministic validation and rebuilds
of integration:

```bash
syncwheel stack resolve-integration feature-a <resolved-commit>
```

The command accepts one or more commits already contained by the integration
branch. It updates `integration_commits` without changing `commits`; stack
rebuilds still use the latter, while integration rebuilds use the former.
When an obsolete source commit is already absorbed by the integration base or
another resolved stack, use `--empty` explicitly rather than inventing a commit.
