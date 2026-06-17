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

## Multi-agent, multi-machine context

When many repositories are maintained by many agents working concurrently, the
shared committed manifest plus the ledger is the coordination point that scales:
every agent reconciles against the same deterministic state, and a fresh agent or
machine recovers with `resume` rather than guessing branch ownership. Per-clone
personal manifests are an overlay on top of that shared base, not a replacement for
it — an all-personal setup has no shared source of truth and diverges at scale.
