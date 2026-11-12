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
changed path. It accepts changes to existing regular files with unchanged modes
and additions of regular files or new directory subtrees. New roots must be
absent from the checkout, including untracked and ignored data. Overlapping
content, deletions, links, submodules, partial clones,
hidden index entries, empty subtrees, effective checkout attributes and filters,
and control-path edits are refused. It does not infer
semantic equivalence. The result retains the entire old integration as
ancestry and keeps all untouched tree entries. A second commit records the
selected stack's projection without changing the source ref or other stack
declarations. This is not a full manifest rebuild, a conflict resolution desk,
or an assertion that another stack's remote PR has landed. Explicit `files`
reference-backend configuration is supported; reftable is refused.

Apply requires Linux, the Git files ref backend and atomic
`RENAME_EXCHANGE` across the actual checkout and Git metadata directories. A
prepared Git transaction holds the HEAD, integration and source ref leases
while the executor aligns its exact files and the real index. A private
journal records both images before checkout writes. An independent Git writer
cannot take the index or held ref locks. Use an exclusive direct writer for the
primary checkout and its metadata: direct filesystem or configuration changes
are not governed by Git locks. An unexpected image aborts; exchanged bytes
remain in the journal. The command never fetches, resets, reaps a lane, pushes,
or recovers another operation. Additions are prepared under that journal before
effects and adopted with atomic `RENAME_NOREPLACE`, never a replacing rename.
An entirely new subtree moves as one unit. Recovery returns it to the journal
only when its root and every descendant file/directory still match their
prepared device/inode and native filesystem export-handle witnesses. Handles
distinguish a deleted/recreated object even when its inode number is reused.
Additions require Linux `name_to_handle_at` support on the actual checkout and
journal filesystem; unsupported filesystems fail during read-only preview,
without a fallback to inode or content hashes. No `open_by_handle_at` or extra
privilege is used. Changed contents or any identically recreated object are
held for operator review. Recovery never deletes concurrent checkout
data. The exclusive-direct-writer prerequisite still applies; Git leases do not
serialize arbitrary filesystem writers.

The handle contract follows the kernel's [exportable filesystem identity
interface](https://docs.kernel.org/filesystems/nfs/exporting.html). Old addition
journals without generation-bearing witnesses are refused; recovery does not
invent an identity from the current checkout.

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

Draft source refs can be created separately through native `stack create --draft`.
Materialization projects objects and creates the ref with a zero-old-OID CAS; it
does not create or remove a checkout. A ref changed immediately after creation
is preserved and reported. A historical temporary checkout is preserved for separate native recovery;
this path never removes it, including when ordinary Git status appears clean.
Ignored data and concurrent writers are not inferred to be disposable. This is
not a network-free publication command: active coordination still reads and
publishes its remote state. It does not relax local replay prerequisites.

## Missing local source refs

`syncwheel stack materialize-local <stack>` previews a ref-only operation for an
already declared exact linear commit sequence, or the exact base of an empty stack. `--apply --plan-digest <digest>
--reason <reason>` creates only its missing source ref with zero-old-OID CAS.
It does not project objects, change the manifest/checkout/index, inspect a remote,
publish, recover other operations, or reap lanes. Active coordination remains
configured and publication still needs its normal remote-state checks.

The primary tracked manifest, complete clean checkout and index, exact local
objects, authority, target and reference hook are witnessed and rechecked under
the shared metadata lock. Source refs checked out anywhere or registered as
governed lane refs are refused. Direct filesystem writers must be exclusive. An
intent and persistent index-lease seed are recorded before CAS. Git holds HEAD,
its branch and the new source ref in the prepared transaction; the seed-owned
index lock also excludes staging writers. The witness is checked under these
leases before committing. An interruption after creation leaves the ref intact.
`stack materialize-local <stack> --recover --plan-digest <digest>` only inspects
the original receipt and releases its own exact seed-owned index lock. Unknown
native ref locks or a foreign index lock are preserved. Uncertain outcomes stay
held, even if the ref is absent; the same ref is never automatically created
again after an intent, including under a new plan digest. A present exact ref
without a pending intent can be inspected without writing it or inferring who
created it. There is no automatic deletion, rewrite, resend or publication.

For a shared delivery clone, `stack create <stack> <commits> --base <base>
--preserve-lanes` only declares local metadata. It never captures, recovers or
reaps any lane and never publishes a draft. Pending control or publication
operations are refused instead of recovered. A configured primary-branch guard
and its configured branch are checked against the primary before stack-creation
control writes. An initially wrong primary or mismatched guard is refused before
staging a declaration; the guard is never retargeted by this operation. Normal
tracking, authority, global metadata locking and branch/lane collision guards
still apply. This flag neither repairs existing staged state nor authorizes a
push; publication and exact source materialization remain separate operations.

The declaration requires a clean primary, Linux/files/SHA-1 topology and explicit
source-change authority. It prepares a manifest-only commit and alternate index
in a private journal, then holds the real index lock and native HEAD/branch
leases across both checkout alignment and the control-ref CAS. A switch before
those leases is detected before manifest/index writes; switches and normal Git
ref writers are excluded while alignment is authorized. The `created` message
is emitted only after the control commit and receipt have completed. Direct
filesystem/configuration writers must remain exclusive, as for local replay.

`stack recover-declaration --plan-digest <digest>` inspects only that journal.
Add `--apply --reason <reason>` to restore its exact pre-commit images or verify
an already committed result. It never moves a ref, retries a creation, removes
unknown Git locks, recovers another operation or reaps a lane. A foreign image,
ref/HEAD drift or foreign index lock is held. The original manifest/index bytes
remain in the journal. This recovery applies only to the new declaration
journal, not to historical staged manifests left by an older command.
Normal completion and recovery share the same operation key and immutable
manifest receipt payload, including the original actor. A crash after ledger
append does not create another `manifest_saved` event on recovery.
Unchanged tracked files may retain group-writable modes during read-only
verification. Their bytes, executable bit and exact permissions are witnessed.
The manifest and index exchanged by the declaration retain the strict image
contract. The operation preserves file permissions.
Ref-only materialization uses the same read-only checkout verification.
Terminal declaration recovery checks the manifest image and index permissions
against its original journal. Declaration startup leaves pending control intents
to their owners and checks them under the metadata lock.

Read-only commands, `int git` and lane-preserving declarations limit exit-time
authorization cleanup to the current process identity and matching nonce that
the invocation actually issued.
Foreign, malformed and recycled-process records remain available to their
separate maintenance lifecycle. Local object acquisition includes the current
process's transient authorization creation and cleanup alongside its Git objects.
