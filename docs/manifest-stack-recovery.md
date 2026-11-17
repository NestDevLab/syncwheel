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
If HEAD already contains the desired manifest bytes, recovery reuses HEAD and records
the compensation without creating an empty commit. A nonempty manifest-only
commit is control metadata in validation and integration reconciliation; it does
not add an undeclared product commit requiring stack classification.

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

## Publishing a shared control change

Local recovery does not publish the integration branch or its local ancestry.
Follow the [manifest maintenance rule](manifest-tracking.md): compare the reviewed
removal with the current remote delivery manifest first. If that manifest already
lacks the declaration and membership, there is no patch to publish for this
withdrawal. Do not copy unrelated local control changes onto the remote base.

When the remote still needs the removal, use a dedicated maintenance PR with an
ignored personal manifest, a distinct integration branch, and the repository's
existing worktree root. Here `main` is the verified remote delivery branch:

```bash
syncwheel init --personal control-maintenance --canonical-remote origin \
  --publication-remote origin --base-branch main \
  --integration-branch integration/control-maintenance/main \
  --syncwheel-tracking git-tracked --no-coordination --worktree-root var/syncwheel
syncwheel stack create control-maintenance --personal control-maintenance \
  --branch maintenance/withdraw-declaration --base BASE_SHA \
  --target-remote origin --target-branch main
syncwheel worktree open control-maintenance --into control-maintenance \
  --base BASE_SHA --personal control-maintenance --json
```

In that governed source lane, prepare and commit only the reviewed declaration
and membership removal on the current delivery base. Restore the single manifest
from the recovery commit only when the base manifest is byte-identical to its
parent; otherwise reapply the semantic removal to the current base. The source
tip must have `BASE_SHA` as its unique direct parent. Return to the primary
checkout, where the personal manifest was created, to register and publish it:

```bash
syncwheel stack set control-maintenance --personal control-maintenance \
  --base BASE_SHA --adopt-tip TIP_SHA
syncwheel stack set control-maintenance --personal control-maintenance \
  --base BASE_SHA --adopt-tip TIP_SHA --plan-digest DIGEST --apply
syncwheel stack push control-maintenance --personal control-maintenance --dry-run
syncwheel stack push control-maintenance --personal control-maintenance
```

Open the maintenance PR against the verified delivery branch using the approved
PR client. The maintenance stack belongs only to the personal manifest, not to
the shared manifest's `integration.stacks`. Follow normal review and checks, then
review `int rebuild --dry-run` through the repository's authorized integration
procedure. A refusal stops this route: there is no raw-push fallback and no reason
to publish the primary integration ancestry merely to deliver this control patch.
