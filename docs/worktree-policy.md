# Worktree preferences

Worktree preferences have two independent fields. A private repository override
takes precedence over the user setting **per field**, then built-in defaults.

| Field | Explicit values | Built-in behavior when unset |
| --- | --- | --- |
| `expiry` | `enforced`, `off` | `enforced` |
| `writer_exclusivity` | `block`, `warn`, `off` | `legacy` |

`legacy` is a reported default, not a value to set. It preserves historical
behavior: no general foreign-writer admission check is added, and the existing
scoped stack preflight requires active associated lanes to be recovered first.
An explicit `block` admits a healthy lane owned by the current actor and refuses
a different writer; `warn` reports the different writer and continues; `off`
does not perform that logical ownership check. These values are opt-ins so an
upgrade with no configuration retains the previous behavior.

## Read, preview and apply

The user commands work outside a Git repository:

```bash
syncwheel self worktree-policy status --json
syncwheel self worktree-policy set --expiry off --writer-exclusivity warn
syncwheel self worktree-policy set --expiry off --writer-exclusivity warn --apply
```

Only supplied fields change. Settings are stored under `worktree_policy` in
`~/.config/syncwheel/settings.json`, alongside update and installation settings.
The existing `SYNCWHEEL_UPDATE_SETTINGS_PATH` override selects this entire
settings file, including worktree preferences. It is useful for isolated tests.

Repository commands inspect or edit the primary checkout's private
`.syncwheel/profile.local.json`; linked worktrees use that same override:

```bash
syncwheel repo worktree-policy status --json
syncwheel repo worktree-policy set --writer-exclusivity block
syncwheel repo worktree-policy set --writer-exclusivity block --apply
syncwheel repo worktree-policy clear writer-exclusivity --apply
syncwheel repo worktree-policy clear --apply
syncwheel self worktree-policy clear expiry --apply
syncwheel self worktree-policy clear --apply
```

`set` and `clear` preview unless `--apply` is present. `clear` without fields
removes both fields at the selected level. Clearing a repository field restores
inheritance; clearing a user field restores its built-in default. Other JSON
keys are preserved. Repository writes require an ignored, untracked profile.
Status reports effective values, their sources, and both configured layers.
These commands do not update lane records, refs, manifests, or coordination state.

Configuration writes share a cooperative document lock and compare the exact
bytes observed by the reader before replacement. This also covers existing
update-mode, installation-identity and repository-profile writers. If another
writer wins, the stale command stops without overwriting its changes; read the
current configuration and rerun the intended update. Concurrent changes are
never silently merged or discarded. Selecting `use --shared` removes only the
personal selection and preserves other profile keys.

## Scope and limits

`expiry off` stops treating a passed lease timestamp as expiration, including
for existing records, diagnostics and expiry-based GC eligibility. Original
timestamps are retained and become effective again when enforcement resumes.
Malformed leases remain errors. Independent recovery of a provably dead local
owner whose worktree is missing remains available.

Explicit writer admission applies to:

- opening a lane associated with a stack that already has an active foreign lane;
- pre-commit callbacks in governed linked lanes, when Syncwheel's hooks are installed;
- the existing scoped lane preflight for stack archive, close, promote, rebuild,
  and integration-resolution capture, and scoped reconcile/resume.

Source publication and capture retain their existing exact-source, ownership and
retention protocols. Commands with an explicit preservation mode retain that
mode's separate contract. Integration operations and unrelated stacks do not
acquire a new foreign-writer gate. Syncwheel does not intercept direct writes to
files, editors or Git commands that do not invoke its hooks.

The two preferences are independent: `writer_exclusivity off` does not permit
an expired lane when expiry is enforced, and `expiry off` does not waive legacy
or explicit writer checks. Neither preference clears a dirty index, Git worktree
lock, interrupted operation, pending cleanup, registration failure or invalid
lease. Registry CAS, lock protocols, publication leases and branch ownership
claims are unchanged.

Writer relaxation does not authorize deletion or taking over another writer's
lane. Automatic capture cleanup continues to protect a known foreign writer;
with expiry disabled, a foreign record's old timestamp cannot release that
hold. `retire-copy` still requires its current known owner, stopped writers,
reviewed digest and content/recovery proofs. Explicit release and GC keep all
their existing physical, archival and destructive-operation checks.
