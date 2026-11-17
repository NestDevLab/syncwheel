# Retire a declared lane copy

A lane can retain dirty or untracked copies of files that are already committed
and declared in its target stack. Publication and release still refuse a dirty
source: matching filenames alone do not prove that its work is preserved.

Preview the exact lane and inspect the selected commit and every path:

```bash
syncwheel worktree retire-copy review-copy --reason "Files already declared"
```

The preview identifies one commit from the target stack's declared history.
Use `--commit COMMIT` to select a particular declared commit when needed. A
commit outside that history is not a valid preservation proof. Keep the same
`--personal` or `--manifest` selection on every command.

Each changed staged entry and each changed working entry must independently
match that commit's raw contents and Git mode. A matching working file cannot
hide a different staged blob. Deletions must agree with the comparison commit;
files cannot be collected from different commits to manufacture a match.
Unsupported data or metadata is refused without cleaning the lane.
Apply requires Linux no-replace rename support. The checkout, Git index, and
recovery directory must share a filesystem; unsupported layouts are refused.

Stop the lane's writers, then apply the exact reviewed digest:

```bash
syncwheel worktree retire-copy review-copy --reason "Files already declared" \
  --plan-digest DIGEST --confirm-writers-stopped --apply
```

Confirmation is an operator assertion that writers have stopped. It does not
override ownership, leases, locks, changed inputs, or missing proof. The command
checks the selected manifest, lane generation, checkout and Git administration,
HEAD, index, and paths again before modifying them.

Recovery is saved outside the checkout before cleanup. The private recovery
location contains separate staged and unstaged binary patches, the original
index, and a tar archive of the verified untracked files. The command verifies
the recovery data before replacing a path. Keep the receipt and recovery files
until their owner has accepted disposal.

Only proven paths return to the frozen lane HEAD. The command does not move
HEAD, rewrite a branch, remove the worktree, release its lease, or remove a Git
retention lock. If the lane is no longer needed, preview its separate release:

```bash
syncwheel worktree release review-copy --reason "Declared copy retired"
```

Review that result before adding `--apply`. If the lane tip differs from the
stack publication tip, release the redundant lane before retrying stack push.
Other release and publication guards still apply.

If cleanup is interrupted, retain the recovery location and retry the same
apply invocation and digest. Recovery must recognize only the exact original
or already-cleaned state. A replacement file, changed index or identity, or an
unrelated lock stops the retry; it is never permission to discard the new work.
