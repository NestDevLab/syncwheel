# Syncwheel

Syncwheel is a deterministic workflow for repositories that keep:
- a canonical upstream
- a publication fork or secondary remote
- one or more `pr/*` review branches
- one `integration/*` branch used for daily development or runtime validation

## Core idea

Work normally on integration.
Do not let integration become a black box.
Every persistent integration change should also belong to a named PR stack.

## Deterministic rule

To make the workflow scriptable, declare the stack model explicitly in:
- `.syncwheel/manifest.json`

That file should describe:
- remotes and canonical base
- integration branch and replay order
- PR stack ids and branches
- exact commits for each stack

## Primary CLI

```bash
python3 scripts/syncwheel.py status --fetch
python3 scripts/syncwheel.py validate
python3 scripts/syncwheel.py plan --json
python3 scripts/syncwheel.py stack sync <stack>
python3 scripts/syncwheel.py stack rebuild <stack> --worktree <path>
python3 scripts/syncwheel.py int rebuild --worktree <path> --reason "refresh integration projection"
```

Mutating `stack push`, `int rebuild`, and `int push` all enter the same
manifest-write classification and recover any pending control-manifest intent
before loading the manifest. Control persistence is ordered as durable intent,
ref CAS, checkout alignment without a ref move, manifest save, then receipt.
`stack push` includes a local integration control tip in its coordinated atomic
publish only when that tip has one persistence receipt and its tree is the exact
current projection. An ineligible housekeeping tip remains local; every ref
selected for publication still passes the shared successor guard.

For an already published integration branch, rebuild first materializes the
declared replay without moving integration. A reconciliation commit preserves
the previous local tip as its first parent and the replay result as its second,
while its tree contains the selected product and control bytes. Its version-2
control intent binds the replay inputs, source and destination leases, and
provenance transition before the single ref CAS. Recovery reuses that exact
object; later checkout or provenance changes leave the intent pending instead
of being overwritten. Existing version-1 control intents retain their recovery
path. A fresh clone recognizes reconciliation through the published state and
the commit's deterministic replay proof, not another clone's local ledger.
If inputs change before the CAS, ordinary recovery refuses the stale intent;
`int rebuild --reason "<reviewed change>"` retires that unapplied intent and
plans from the current selection. This escape does not abandon an intent
whose ref CAS already landed.

## What becomes deterministic

With the manifest in place, the script can tell you:
- which commits belong to each stack
- whether the stack branch contains them
- whether integration contains them
- which branches need to be rebuilt
- in what order integration should be replayed

## What the AI should do

The AI should not guess the stack model from memory when the repo is meant to be maintained by syncwheel.

Instead, it should:
1. run the script
2. validate the manifest
3. rebuild branches from the manifest
4. validate again
5. report honestly

## Why this matters

Without a manifest, syncwheel is still possible, but partly heuristic.
With a manifest, syncwheel becomes repeatable and much easier to automate safely.
