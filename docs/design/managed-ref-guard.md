# Managed-ref push guard

Syncwheel owns publication of every ref declared by its repository model. A raw
`git push` can otherwise advance one of those refs without its manifest/state
transaction, as happened when an integration ref advanced independently of the
coordination state.

## Local contract

`syncwheel hooks install` is plan-only; `--apply` installs. The command honors
`core.hooksPath`. When `pre-push` already exists, Syncwheel moves it to a stable
chain path and runs it before the managed-ref check. An ownership sidecar records
the generated hook digest and chained-hook digest. Removal is also plan-first,
refuses modified/unowned hooks, and restores the chained hook.

The policy is required-by-default for `git-tracked` clones with managed refs,
active-active coordination, or an owned journal branch. New setup flows include
the hook action in their plan and install it visibly on apply. Existing clones
enter an explicit migration-pending state: validation warns without rewriting
hooks, and applying installation activates fail-closed enforcement for later
Syncwheel mutations. `local-only` contribution clones are optional. A clone can
persist `hooks.mode=disabled` only through `hooks remove --disable --reason ...`;
the reason remains visible in status and validation.

The guard reads Git's canonical pre-push input, so the destination ref is checked
after Git resolves shorthand. It therefore covers direct and indirect refspecs,
multiple updates, deletion, force, same-ref aliases, and `HEAD:<managed>`.

Protected refs are derived at push time from:

- manifest integration, stack/draft source, and channel branches;
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
or running on a different clone bypasses it. A
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
