# Withdraw a ledger-owned control declaration

A stack created in the shared manifest can leave that control file dirty after
`stack sync`. Ordinary publication and archive commands refuse that state. If the
only change is the exact declaration recorded by native stack operations, inspect
an explicit withdrawal plan from the primary checkout:

```bash
syncwheel manifest recover-stack example --reason "Declaration belongs in a personal manifest"
```

Review the stack, current primary HEAD, source tip, ledger evidence, preserved
commits and recovery refs. The plan removes the selected declaration and its
integration membership, including an earlier generation already committed in
HEAD. It does not undo product commits. Apply only the reviewed digest, keeping
the same reason:

```bash
syncwheel manifest recover-stack example --reason "Declaration belongs in a personal manifest" \
  --plan-digest DIGEST --apply
```

The command proves the complete manifest difference against HEAD and the native
ledger, retains all proven source generations, and creates a control commit that
changes only the manifest. A compensating ledger snapshot withdraws the stack
while retaining the original events. Source branches, foreign lanes, product
files, ignore files and remote refs remain unchanged. Nothing is pushed.

This recovery requires the canonical, git-tracked, non-coordinated delivery
manifest in its primary checkout. Staged changes, unrelated dirty files, manual
manifest edits, missing history, dependencies and unproven source movement refuse.
A matching stack ID alone is not ownership proof. Preview is observational and
does not create Git objects or recovery files.

Native publication or merge receipts and existing remote-tracking publication
refs also refuse. This is an offline recovery: missing local evidence does not
prove current remote absence. Inspect publication history before withdrawing a
declaration; the command preserves remote and source refs in every case.

An interrupted apply retains its original plan and explicit recovery intent.
Repeat the same command with the same digest to finish only that transaction.
Other commands must not resume it implicitly. Concurrent changes still refuse;
preserve them and inspect the reported conflict rather than editing the receipt,
restoring the manifest manually or bypassing a guard. After success, inspect
`status` and `ledger show` before the normal delivery workflow.

`manifest recover-control` handles a different case: a native replay backup and
an unchanged replay manifest. It is not an undo command for an edited declaration.
