# Witnessed local integration replay

`syncwheel int replay-local <stack>` previews a local-only adoption. It prints a
digest and witness and creates no checkout, lock, ref, object, cache, or journal.
Apply is a separate operation:

```text
syncwheel int replay-local <stack> --apply --plan-digest <digest> --reason "<reason>"
syncwheel int replay-local <stack> --recover --plan-digest <digest>
```

This is an intentionally narrow path for a tracked, non-coordinated delivery
manifest with source-change authority and a checked-out cherry-pick integration
branch. Use the primary checkout and locally available exact commit IDs for the
integration base, stack base and stack commits. The selected source must be an
exact linear chain on its declared branch, and cannot already have an
integration resolution. Neither branch may be a symbolic ref. Existing
managed reference hooks must be exact, active, unchained and protect the target;
custom reference hooks are refused.

The witness requires identical source-base and integration blobs on every
changed path. It accepts changes to existing regular files with unchanged modes;
overlapping content, additions, deletions, links, submodules, partial clones,
hidden index entries, empty subtrees, effective checkout attributes and filters,
and control-path edits are refused. It does not infer
semantic equivalence. The result retains the entire old integration as
ancestry and keeps all untouched tree entries. A second commit records the
selected stack's projection without changing the source ref or other stack
declarations. This is not a full manifest rebuild, a conflict resolution desk,
or an assertion that another stack's remote PR has landed.

Apply requires Linux, the Git files ref backend and atomic
`RENAME_EXCHANGE` across the actual checkout and Git metadata directories. A
prepared Git transaction holds the HEAD, integration and source ref leases
while the executor aligns its exact files and the real index. A private
journal records both images before checkout writes. An independent Git writer
cannot take the index or held ref locks. Use an exclusive direct writer for the
primary checkout and its metadata: direct filesystem or configuration changes
are not governed by Git locks. An unexpected image aborts; exchanged bytes
remain in the journal. The command never fetches, resets, reaps a lane, pushes,
or recovers another operation.

Recovery never replays or retries a ref move. If alignment did not start,
it can close the intent and release its own index lock even if another writer
advanced the branch. With alignment started and the old tip still current,
it restores only journal-owned images. With the new tip current, it verifies
the already aligned checkout and closes the receipt. If the ref, checkout, or
journal is inconsistent, it retains evidence and refuses. A full process-group
kill may leave native Git ref lock files with no ownership proof. Recovery
reports that obstruction and leaves the locks intact; an operator must establish
their ownership before any separate repair. The journal stays under the
repository's private Git metadata.
