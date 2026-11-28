# Attributed delivery recovery

This optional native PR gate implements the recovery rule in
[AI agent delivery boundaries](ai-agents.md#delivery-boundaries-and-violations).
It does not change forge approvals, authority, CI, protected branches, or deploy
permission. Without owner opt-in, existing commands retain their behavior.

## Ownership and trust

An owner enables the gate in the repository's private profile, separately from
the candidate. For example:

```json
{
  "actor_recovery": {
    "enabled": true,
    "repository": "example/project",
    "remote": "origin",
    "state_ref": "refs/heads/delivery-recovery-state",
    "authority_ref": "refs/heads/delivery-recovery-authority",
    "product_refs": {"refs/heads/main": "project-release"}
  }
}
```

Both dedicated refs must already exist on the approved remote. They contain
only `.syncwheel/actor-recovery.json`. Protect them server-side: the independent
maintainer controls authority records, the authorized recovery writer appends
state, and candidates cannot rewrite either history. The authority document is
append-only by record ID; correcting an attribution requires an owner-reviewed
reconciliation, never replacing a record. The local profile is owner configuration,
not a security boundary against arbitrary local edits or external Git clients.
Do not enable this gate where the repository cannot provide those protections.

Authority documents have `schema_version: 1`, `repository`, and `records` keyed
by immutable reference ID. Initial state is `empty_state(repository)` from
`syncwheel_actor_recovery`, with empty `incidents`, `operations`, and
`reviewed_through` maps. Provisioning and authority record creation are deliberate
owner operations, outside the candidate's CLI. A missing store refuses operations;
it is never treated as empty history. Two clones read the same remote refs;
state appends use exact remote leases and preserve ancestry. Stale writers
re-read and reapply their intent; there is no automatic merge or retry.

The maintainer verifies identity through the project's authenticated agent/session
binding and the original work evidence. `USER`, forge account, device, reporter,
lane owner, local receipt, and an agent's own claimed name do not prove the
responsible agent. Stable actor IDs follow that agent across lanes. A successor
has their own ID and inherits no review obligation. An opt-in publication needs
an independently attested binding, including for an agent without incidents;
unknown identity never creates an incident or a global penalty.

## Evidence records

Every record includes `kind` and the exact `repository` identity.

| Kind | Required evidence and identity |
| --- | --- |
| `incident` | Canonical distinct `incident`, stable `actor`, affected `delivery`, `proven: true`, `attributed: true`, and `evidence` reference to independently verified facts |
| `recovery` | Existing `incident`, `recovered: true`, `attribution_verified: true`, and verified `evidence` |
| `publication` | `operation`, stable `actor`, `delivery`, exact `head`, `pull_request`, `base_branch`, and exact `base_sha` |
| `review` | Same `operation`, `actor`, exact `head`, different `reviewer`, `outcome: "approved"`, and verified `evidence` |
| `outcome` | Same `operation`, `actor`, `delivery`, exact `head`, `outcome: "published"` or `"not_published"`, and verified remote `evidence` |

Warning, discovery, suspicion, failed diagnostics, and reporting are not incidents.
The authority deduplicates the underlying incident, rather than assigning a new
ID to each report. Replaying the same confirmed incident cannot increment the
count. Local JSON imports and unverified booleans cannot create authority records.

Only unrecovered incidents for the selected delivery block that delivery. One
incident does not require repeat review. After the second distinct confirmed
incident, the next actual publication of that actor requires independent review
of the exact candidate. It applies across lanes. Verified recovery removes the
delivery hold; it does not erase incidents or the repeat-review obligation.

## Native commands

```bash
syncwheel repo actor-recovery status
syncwheel repo actor-recovery confirm --reference incident-proof
syncwheel repo actor-recovery confirm --reference incident-proof --apply
syncwheel repo actor-recovery recover --reference recovery-proof --apply
```

Commands preview by default and always emit JSON. They change only the dedicated
state ref, without changing the manifest, application refs, index, or working tree.
Normal PR preview, plan digest, CI, authority, and forge gates still apply:

```bash
syncwheel stack merge-pr feature --operation-id publication-42 \
  --actor-publication exact-publication-binding --actor-review exact-review
syncwheel stack merge-pr feature --operation-id publication-42 \
  --plan-digest DIGEST --actor-publication exact-publication-binding \
  --actor-review exact-review --apply
```

Review is needed only when that actor owes repeat review. The publication binding
must match the actual PR, base, candidate and operation; changing lane or stack
does not change the actor's state. A durable reservation precedes the forge effect.
Only one unresolved publication per actor is allowed; other actors remain free.
Review debt is consumed only on an exact remote merged observation with merge
identity and time, never on preview, passing tests, a command exit code, or work-ref
synchronization. New incidents confirmed after reservation remain outstanding.

## Interrupted publication

Do not repeat a merge after an uncertain result. Native reconciliation reads the
original binding and observes that exact PR without another merge:

```bash
syncwheel repo actor-recovery reconcile --reference exact-publication-binding --apply
```

This also works after interruption between reservation and ordinary PR ledger
persistence. Exact successful readback is idempotent. An open PR at its observed
base proves no publication and releases the reservation without consuming review;
a retry uses a new operation and independently attested binding. Missing, changed,
or uncertain remote identity keeps the reservation. A separately verified owner
outcome can be recorded with `complete --reference outcome-proof --apply`.

## Covered publication and residual surfaces

`stack merge-pr` is product publication to the explicitly mapped target. Syncwheel's
common Git runner refuses an alternative push to that target, including stack,
integration, channel or reconcile publication that would write it. Explicit pushes
to other work refs synchronize work and remain ordinary operations. Ambiguous
default or wildcard pushes on the configured product remote refuse until their
scope is explicit. Other remotes and unmapped product targets are outside this
opt-in contract. Do not map work refs as product targets merely to record warnings.

Raw Git, another forge client, CI release jobs, external deployment and runtime
installs are not intercepted. Server protection and their owners must enforce
the same contract before claiming end-to-end protection there. A local CLI cannot
authenticate arbitrary agents, secure another clone, or atomically combine forge
merge and Git state push. Reservation is the authorization boundary; a crash or
later observation failure retains uncertain state for native reconciliation.
