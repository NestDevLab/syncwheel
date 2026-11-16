# Adopt an existing stack history

Use history adoption when a stack already has the commit graph you want to
deliver. For example, a governed lane may contain a merge that brings the
delivery base into the source branch. Ordinary `stack add` records commits for
replay and can refuse a merge whose projection parent it cannot identify.
Adoption expresses the existing graph through Syncwheel and replaces manual
manifest repair for this case.

Preview the operation with the reviewed base and tip. Use full commit IDs so the
same inputs identify the same objects when you apply.

```bash
syncwheel stack set STACK --base BASE --adopt-tip TIP
```

The preview prints JSON with the resolved `base` and `tip`, the current local
source tip (`oldTip`), the locally observed remote source tip
(`observedRemoteTip`), the complete declared history (`history`, all commits
in `BASE..TIP`), and `planDigest`. It reads existing Git objects and worktree
registrations. Apply the digest from that preview.

```bash
syncwheel stack set STACK --base BASE --adopt-tip TIP --plan-digest DIGEST --apply
```

For a nonempty history, the base must be the tip's unique direct projection
parent. In Git terms, the only direct parent of the tip reachable from the base
must be the base itself. An empty history can use the same commit for base and
tip. The exact tip must preserve ancestry from both the current local source tip
and the locally observed remote source tip, where those refs exist. Syncwheel
refuses overlap with another stack's declared history, symbolic source refs,
and movement of a source branch checked out in any worktree.

Apply records the exact existing tip and stores the typed `adopted_history`
field with full `base` and `tip` commit IDs. Public coordination snapshots
preserve that field when sharing and restoring stack state. Validation treats
the entire `BASE..TIP` graph as declared stack history. The source ref advances
locally to the reviewed commit without replay or reconstruction. Adoption
leaves worktree registration and cleanup to their own commands.

Review a new tip or base through another explicit adoption. `stack sync` and
`stack absorb` refuse to change an adopted history, and automatic reconciliation
refuses to rebuild or realign its source branch. If resume finds an undeclared
integration commit likely owned by the adopted stack, it requests manual review
and preserves the adopted history.

Publish separately through `syncwheel stack push STACK`. A clean active lane
registered to the stack can serve as its publication source when its HEAD
exactly matches the declared stack tip and its commits belong to that stack.
This requires no capture or ownership change. You can also use
`stack push STACK --retain-captured-worktrees REASON` to explicitly retain the
active lane. Either form preserves the lane. Keeping a previously captured
copy still requires the explicit retention option and a Git worktree lock.
Channel pins and `stack land` currently reject adopted histories.

## Resume an interrupted apply

Inspect the ledger and current source ref and manifest before resuming. Run
the same apply command with the original pinned base, tip, and digest.
Syncwheel holds other mutations while the prepared operation is unresolved.
It resumes only when the observed manifest and source ref match a valid stage
of the original operation. If they differ, stop and report the refusal and the
observed state. Use Syncwheel's recovery path and preserve the repository state
for investigation. Raw pushes, manual manifest edits, and internal mutation
helpers bypass the recorded evidence.

Coordinated publication uses state schema 5 when an adoption is present. Older
clients refuse that state instead of republishing it without the adoption binding.
Update clients before they operate on a local manifest containing an adoption.
