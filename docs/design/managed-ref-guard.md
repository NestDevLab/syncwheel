# Managed repository guards

Syncwheel owns publication of every ref declared by its repository model. A raw
`git push` can otherwise advance one of those refs without its manifest/state
transaction, as happened when an integration ref advanced independently of the
coordination state.

## Local contract

`syncwheel hooks install` is plan-only; `--apply` installs the `pre-push`,
`post-checkout`, `pre-commit`, and `reference-transaction` bundle. The command honors `core.hooksPath`.
When a hook already exists, Syncwheel moves it to a stable chain path and runs it
after its own check. The guard and user hook both run, and a failure from either
rejects the operation. Per-hook ownership sidecars record the generated and chained
digests. Removal is also plan-first, refuses modified/unowned hooks, and restores
every chained hook.

The policy is required-by-default for `git-tracked` clones with managed refs,
active-active coordination, or an owned journal branch. Required means status and
validation report an absent, stale, or tampered bundle; it does not make hook
installation an implicit prerequisite of ordinary Syncwheel commands. Install or
repair it explicitly with `syncwheel hooks install --apply`. Explicit
`hooks status|install|remove` lifecycle commands retain their observational or
plan-first semantics, and generated hook callbacks are excluded to prevent
recursion. A foreign hook is chained rather than overwritten. `local-only`
contribution clones are optional. A clone can
persist a disabled guard only through `hooks remove --disable --reason ...`; the
reason remains visible in status and validation.

The clone has one effective guard target, not one hook bundle per profile.
`hooks install|remove|status` resolve the selected shared, `--personal`, or
`--manifest` manifest consistently. Installation records that manifest's integration
branch; disable intent is appended to that manifest's ledger. Status against another
profile, or after an integration-branch rename, is `degraded` and directs the operator
to rerun `hooks install --apply` with the same profile selector.

Generated hooks invoke a stable installed `syncwheel` CLI resolved at installation,
never a path inside the repository, Git common directory, `var/`, the configured or
default lane root, or any registered worktree. Non-executable shims are rejected.
Hooks fail closed if the stable CLI is unavailable. Their common Git directory stores
the integration branch and opt-out state in one authoritative `guard.json`, so a
missing or older working-tree manifest and stale profile data cannot disable the guard.
Every read validates schema version 1, a non-empty `integrationBranch`, boolean
`enabled`, and a non-empty `reason` when disabled. Missing, unreadable, malformed,
or incomplete state makes installed hooks fail closed and makes status report
`degraded` with the exact repair cause; it never becomes an implicit opt-out.

`guard.json` is written through a same-directory temporary file and atomic rename.
Re-enabling writes it before installing the hook bundle. A partial install therefore
leaves enabled state with `degraded` status and an exact missing/stale/tampered-hook
cause. A manual hook edit is degraded even while its structural status remains
`conflict`. Disabling appends the `primary_guard_disabled` ledger intent, including
actor and reason, before removing hooks and persists disabled state only after removal.
An audit failure leaves the enabled state and hook bundle untouched.

## Primary checkout

The primary worktree is a shared integration projection, not an authoring desk. The
`post-checkout` guard compares that worktree with the declared branch and returns a
visible failure if a checkout moved it elsewhere. Git has already completed the
switch when this hook runs, so the guard deliberately does not reset, clean, stash,
or switch anything automatically. The `pre-commit` guard blocks both a mismatch
and a manual commit while the primary is correctly on integration. Syncwheel's own
control and in-place rebuild commits pass through a short-lived, single-use nonce
used for managed ref moves. The nonce binds the PID and its process-start identity,
so a recycled PID cannot inherit the capability. Cleanup removes only nonces owned
by the current process, a process that is no longer alive, or a provably recycled
PID, so concurrent Syncwheel processes keep their capabilities. A malformed or
unreadable nonce is retained for the TTL to avoid racing a writer, then removed only
after a durable event is appended under the Git common directory. The
reference-transaction hook blocks every unauthorized
integration-ref move, including fast-forward moves. Both guards allow commits in dedicated feature worktrees
and plumbing-materialized branches.

The refusal names `syncwheel worktree open <lane> --into <stack>` for new work and
`syncwheel stack capture-integration <stack> HEAD` for work already committed on
the primary. The only deliberate local escape hatch is the reasoned, clone-local
`syncwheel hooks remove --disable --reason "..." --apply`; its reason remains
visible. This is an operational recovery path, not an identity boundary.

Before a built-in mutation starts, Syncwheel also checks the primary working tree.
Tracked changes stop the operation before side effects and name the same remedies.
Those remedy commands themselves bypass this preflight so recovery remains possible.
Read-only commands continue; on a TTY they show a yellow warning with the dirty-file
count and state that the shared primary changes are not owned by the invoking user.
The parser's exhaustive command behavior table is the single mutation classifier.
It declares flag-sensitive mutations such as `--apply`; previews and read-only
commands are not blocked. A source-scanning test covers both the CLI and revision
provider, including method-based journal savers, and requires every command that can
reach a manifest, ledger, guard-state, or provider-journal saver to have mutation
metadata. The recovery-remedy set is asserted exactly rather than only positively.

Git has no pre-checkout hook, so preventing the ref move itself is not portable.
The combination of immediate post-checkout failure, commit blocking, validation,
and Syncwheel's integration-first workflow is the strongest reversible local guard
available without wrapping the `git` executable.

## Managed-ref rewinds

`pre-commit` decides which branch may be committed to and `pre-push` decides what
may leave the clone, but neither observes a branch being moved backwards. A plain
`git reset --hard` onto a non-descendant commit therefore drops committed work
from a managed branch without any guard reporting it, and the loss only surfaces
later as an unexplained divergence from the published tip.

The `reference-transaction` hook closes that gap. In the `prepared` phase it
refuses every unauthorized update to the configured integration ref, including a
fast-forward, rewind, creation, or deletion. Unmanaged branches are untouched, so
ordinary Git use outside the shared integration projection is unaffected.

Syncwheel rebuilds managed branches legitimately, so every Git process it spawns
carries an authorization variable. That authorization is injected per child
process and never into Syncwheel's own environment, so it cannot leak to an
unrelated caller sharing the process.

Every generated hook fails closed. A missing stable installed CLI or guard
configuration blocks the transaction and is reported as degraded by `hooks status`.
Normal policy refusals print only their refusal and remedy; the wrapper does not
misdiagnose a non-zero policy result as a broken CLI.
The installed bundle is explicit: `pre-push` guards publication, `pre-commit` guards
manual primary commits, `post-checkout` reports primary branch mismatch, and
`reference-transaction` guards every integration-ref update. A local hook is a safety guard, not a security boundary:
`core.hooksPath`, `--no-verify`, and a fresh clone all bypass it.

## Managed-ref publication

The guard reads Git's canonical pre-push input, so the destination ref is checked
after Git resolves shorthand. It therefore covers direct and indirect refspecs,
multiple updates, deletion, force, same-ref aliases, and `HEAD:<managed>`.

Protected refs are derived at push time from:

- manifest integration, stack/draft source, and channel branches;
- the base branch and every stack's landing target, which only `stack land`
  may publish;
- the coordination-state branch and all refs still owned by its latest state;
- the journal branch when repository mode is `journal`.

Syncwheel publishers create an authorization file under the Git common directory.
It is mode `0600`, expires quickly, is single-use, and binds the exact remote and
allowed destination refset. Git may omit same-tip refspecs before invoking the
hook, so the submitted set may be a strict subset of that transaction scope.
This is a process-safety capability, not protection from a
host user who controls the repository.

## Server-side hardening

The hook is not a security boundary: `git push --no-verify`, deleting the hook,
or pushing from a clone before its first normal Syncwheel command bypasses it. A
[GitHub ruleset](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
can restrict updates to stable ref-name patterns and grant bypass to a GitHub App,
but ruleset patterns
cannot be derived dynamically from a Syncwheel manifest/state on each push.

A stronger design is a dedicated GitHub App publisher plus rulesets that deny
managed-ref updates to humans and generic automation while allowing only that App.
The App must validate a signed Syncwheel transaction containing the manifest/state
digest, expected-old tips, and exact refset before performing the server-side update.
Dynamic ref ownership changes must update rulesets through a separately audited
control-plane transaction using the
[repository rulesets API](https://docs.github.com/en/rest/repos/rules). Until that
exists, rulesets are defense in depth and
the local guard remains a correctness aid, not an authorization boundary.
