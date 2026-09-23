# Manifest tracking policy

Whether `.syncwheel/manifest.json` should be committed is a **repo-local
Syncwheel policy**, persisted in the manifest as `syncwheel_tracking`.

Supported values:

- `git-tracked`: `.syncwheel/manifest.json` is part of the repository's shared
  coordination contract and should be tracked by Git.
- `local-only`: Syncwheel metadata is per-clone and should stay out of the
  repository's committed files.

Run this before branch, push, PR, or migration work:

```bash
syncwheel repo tracking status
```

If `syncwheel_tracking` is missing, do not guess permanently. Choose a mode with
the maintainer/user, then persist it:

```bash
syncwheel repo tracking set git-tracked --apply
syncwheel repo tracking set local-only --apply
```

The usual choice is still based on who controls the repository, but ownership is
only the input to the decision. The durable truth is the manifest policy.

## `git-tracked`: shared manifest

Commit the shared manifest so the team versions it:

- track `.syncwheel/manifest.json`
- keep local metadata ignored through the Syncwheel-managed `.gitignore` block:
  `.syncwheel/ledger/`, `.syncwheel/manifests/*.local.json`,
  `.syncwheel/profile.local.json`, and `.syncwheel/wt/`

**Benefits**

- the stack and integration topology is **versioned and shared** — every clone and
  every agent inherits the same deterministic plan
- reproducible across machines with no out-of-band setup
- the manifest becomes the team's **coordination contract**: branch ownership is
  reviewable in-tree

For a newly initialized `git-tracked` repository, Syncwheel enables manifest
version 2 active-active coordination when a configured publication remote is
provided. That remote is the shared publication boundary; the manifest and
append-only remote state remain reviewable without recording local paths or
identity details. If no usable remote exists, initialization fails rather than
silently selecting a single-device fallback.

**Manifest self-reference rule.** Treat manifest edits and Syncwheel-version bumps
as control-plane metadata, not as normal stack-owned product commits. A manifest
cannot cleanly name the SHA of the commit that edits itself. Keep manifest
maintenance in an admin checkout or a dedicated maintenance PR that is intentionally
excluded from `integration.stacks`; rebuild PR branches and integration from the
manifest, then validate again.

## `local-only`: untracked manifest

When Syncwheel metadata should not be proposed to an upstream maintainer, keep it
local:

- exclude `.syncwheel/` via `.git/info/exclude`
- default worktrees live under `.syncwheel/wt/`, covered by that `.syncwheel/`
  exclude
- do **not** modify the committed `.gitignore`

**Benefits**

- you still get worktree isolation, stacks, deterministic reconcile, and the
  append-only ledger
- you do **not** impose Syncwheel config on a maintainer who may not use it
- your PRs stay clean — only the real change is proposed, with no tooling noise
- coordination and recovery happen via the canonical remote plus `resume`

**The manifest never enters the integration tree.** A `local-only` repository
without active-active coordination gets no `chore: restore Syncwheel control
manifest` commit after an integration rebuild; the rebuilt replay tip is the
integration tip, and the ledger receipt names that tip as the control commit. An integration tree without the manifest
is the convergent state, so reconcile plans neither a rebuild nor a push to
reach control-manifest parity with a remote.

A control commit left by an earlier release is dropped the next time the
integration branch is rebuilt. Before the reset, Syncwheel records the bytes of
every manifest source the reset can reach and untracks `.syncwheel/manifest.json`
in the checkout that holds the integration branch; afterwards it restores only
the sources the reset removed. The file therefore survives byte for byte for a
default, `--manifest` or `--personal` path, and no deletion is staged in a
checkout that is on another branch. The previous tip stays reachable through the
automatic `backup/<branch>-before-syncwheel-<timestamp>` ref. A control commit
that was already pushed is never removed automatically: republishing the
rebuilt branch needs a reviewed
`syncwheel int push --remote <remote> --force-with-lease`, because a plain
push is not a fast-forward.

**Where the manifest may be pushed.** Every Syncwheel push checks each ref it
updates before anything is sent. A commit whose tree carries
`.syncwheel/manifest.json` may only go to the integration branch, never when
that branch is `defaults.base_branch` on the canonical remote, or to a Syncwheel
coordination state or claim ref. Stack, pull-request, channel and landing
branches are refused, whichever command builds the push. The canonical remote is
matched by its push URL, not its name. A manifest the integration base already
carries unchanged is a product file and is not restricted.

**Active-active coordination keeps the control commit.** Its state commit binds
the manifest by reading it from the published integration tip, so a coordinated
repository still gets one and publishes it on its integration branch, whatever
the tracking policy says. The push rule above applies to it unchanged.

**A manifest the base tracks is not a control commit.** When the projection
itself carries `.syncwheel/manifest.json` — an upstream that versions its own
manifest — that file is a product file. The integration branch stays convergent
and still publishes; settle the tracking policy with `syncwheel repo tracking
status` if that is not what you want.

`local-only` is deliberately not auto-enrolled in active-active coordination.
It can opt in only with an explicit remote and apply step:

```bash
syncwheel coordination init --remote origin --apply
```

This keeps an untracked manifest from unexpectedly creating shared remote state.
When that remote is also `defaults.canonical_remote`, `validate` warns and names
the state branch, because the coordination state is then published on a remote
the repository does not own.

## Migration

Use `repo tracking set` to migrate between modes:

- `local-only -> git-tracked`: writes the manifest policy, adds the managed
  `.gitignore` block, removes the managed `.git/info/exclude` block, and stages
  `.syncwheel/manifest.json`.
- `git-tracked -> local-only`: writes the manifest policy locally, removes the
  managed `.gitignore` block, adds the managed `.git/info/exclude` block, and
  removes `.syncwheel/manifest.json` from the Git index with `git rm --cached`.

The CLI only edits Syncwheel-managed blocks. If `.gitignore` contains manual
`.syncwheel/` ignore entries outside the managed block, `repo tracking set
git-tracked --apply` stops and asks for manual audit.

Tracking-policy migration does not silently upgrade a legacy manifest to
active-active coordination. After confirming the publication remote and shared
ownership boundary, opt in separately:

```bash
syncwheel coordination init --remote origin --apply
```

An explicit opt-out remains persisted in the version 2 `coordination` block and
can later be re-enabled through the same command. See
[active-active-coordination.md](design/active-active-coordination.md) for the
protocol details.

## Multi-agent, multi-machine context

When many repositories are maintained by many agents working concurrently, the
shared committed manifest plus the ledger is the coordination point that scales:
every agent reconciles against the same deterministic state, and a fresh agent or
machine recovers with `resume` rather than guessing branch ownership. Per-clone
personal manifests are an overlay on top of that shared base, not a replacement for
it — an all-personal setup has no shared source of truth and diverges at scale.
