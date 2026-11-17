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

Review a new tip or base through another explicit adoption. When the manifest,
pinned commits and direct source ref still match the adoption, `stack sync`
succeeds without changing anything: the stack is adopted and has nothing to
sync. It prints integration rebuild and stack push previews as next steps.
A missing, symbolic or changed source ref remains a stale adoption and requires
explicit review of the base and tip. Pending adoption, control recovery and
promotion operations must still be resolved before synchronization.

`stack absorb` cannot edit pinned adopted history. For a consistent adoption it
leaves the selected patch intact and names absorption into an existing,
non-adopted draft, using `-N -m MESSAGE` for a new commit. Select a draft you own
and keep the printed manifest and path selection. Existing primary ownership and
staged-path guards still apply: unrelated staged work must be preserved for its
owner, and can block this follow-up. This refusal is not a request
to repeat the completed adoption, and does not claim the patch was absorbed.
A stale adoption retains the explicit base/tip review remedy.

Automatic reconciliation refuses to rebuild or realign the adopted source
branch. If resume finds an undeclared
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

## Deliver an equivalent candidate while preserving the lane

When an adopted candidate differs in history from a preserved lane, qualify
and publish it explicitly:

```bash
syncwheel stack push STACK --retain-captured-worktrees "Keep reviewed source" \
  --allow-equivalent-candidate --dry-run
syncwheel stack push STACK --retain-captured-worktrees "Keep reviewed source" \
  --allow-equivalent-candidate
```

The stack must already adopt the exact candidate tip. Its complete Git tree
must equal the preserved lane tree, including file modes, symlinks and binary
objects. This deliberately accepts exact tree equivalence only; patch-id
similarity alone cannot prove that content remains present. The candidate
must descend from the current published remote head, which must exist and be
available locally. Fetch and inspect an unknown remote head before retrying.
Caller force and passthrough push options are refused. The legacy transport
uses an internal exact lease after proving fast-forward ancestry, so a remote
deletion or advance between verification and push fails atomically. This lease
never authorizes replacing remote history.

Every selected lane must retain its native registration, generation and clean
source state; captured lanes also need completed native capture evidence.
Both active and captured lanes require an explicit Git worktree lock in this
mode. Interrupted cleanup, dirty or missing lanes, tree differences and stale
observations refuse publication. The existing retention path still requires
identical heads unless this option is supplied.

The push uses the pinned candidate SHA through the normal authorized pre-push
hook and coordinated publication path. It never moves or deletes a lane ref,
worktree or registry entry. A `preserved_candidate_delivery_prepared` ledger
event records the reason, preserved ref and tip, candidate tip, observed remote
head and exact tree before publication; `stack_pushed` carries the same proof
on success. A prepared event alone does not prove a successful push.

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
