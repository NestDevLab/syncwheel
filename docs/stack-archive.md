# Defer a stack with retained history

`stack archive` removes a stack declaration and its integration membership
without claiming delivery, ancestry, or patch absorption. Use it when work is
being deferred and must survive rebuilding integration.

Preview, review the exact endpoints, then apply the same request:

```bash
syncwheel stack archive feature --reason "Deferred pending review" \
  --resume-intent "Review retained commits before adding them to integration"
syncwheel stack archive feature --reason "Deferred pending review" \
  --resume-intent "Review retained commits before adding them to integration" \
  --plan-digest DIGEST --apply
```

The preview is read-only. It binds the complete manifest generation, local
source tip, each declared source and integration commit (including a resolved
integration replacement), coordination state, managed remote refs, and the
source claim. Missing remote objects require an explicit fetch or handoff before
planning. Active writer lanes, source checkouts, locks, dependent stacks, and
referencing channels must be resolved first. There is no force option.

Apply atomically creates immutable local `refs/archive/<source-epoch>/<stack>/
<generation-digest>/<endpoint>` refs. An existing endpoint is accepted only at
its exact intended SHA. Every declared commit must be reachable from a retained
ref before the native close protocol runs. Source branches and remote refs are
preserved. Product files and `origin/main` are unchanged; the ordinary control
manifest commit may advance local integration.

The local `stack_archive_prepared` event and the close intent/receipt record the
plan digest, full endpoint IDs, source generation, reason, resumption intent,
retention timestamp, and a 30-day minimum retention requirement. Archive refs
are never automatically expired. Archived source branches are excluded from
tombstone garbage collection, including in other clones; removal requires a
separate explicit recovery review after the retention period. Local archive refs
are not remote backups: retain the clone and its existing remote source branches.

Active-active archival uses the existing exact-lease claim tombstone and
append-only coordination state publication, then saves the local manifest. It
does not delete remote branches or claims. The inert state remains evidence.
A publication failure preserves the declaration and archive refs. Retry the
exact command and digest: a landed close is recovered from its operation token
without republishing, while a changed generation or lease fails closed. A
completed request is idempotent and cannot archive a newly recreated stack.

Inspect `syncwheel ledger show` for the receipt. To resume, inspect the retained
commits and create a new native stack with the reviewed endpoints; archival does
not automatically restore or apply deferred content.
