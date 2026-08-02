# Lossless checkout repair

Use this protocol when the primary/admin checkout is dirty or on the wrong
branch, or when dirty work must move between worktrees.

1. Inventory branch/upstream, worktrees, stashes, staged and unstaged binary
   diffs, untracked files as `<relative path, Git blob>`, and every nested
   repo/submodule state. Run Syncwheel tracking/status/check and understand the
   dry-run projection. Completion: every state category has a reproducible
   snapshot.
2. Create named recovery refs and include-untracked stashes; snapshot dirty
   submodules separately. Never reconcile from a stale projection. Completion:
   named refs protect every commit and stash payload without the original
   checkout.
3. Create or relocate the dedicated worktree under the declared root, then
   restore index, worktree, untracked files, and submodules. Linked worktrees may
   give submodules separate object stores; if a stash is unusable, transfer its
   index (`<stash>^1..<stash>^2`) and worktree (`<stash>^2..<stash>`) binary
   patches separately, then restore untracked blobs. Completion: staged and
   unstaged patch digests plus untracked path/blob sets match the snapshot.
4. Only after proof, leave the primary checkout clean on `integration.branch`.
   Retain all recovery artifacts until every active lane is delivered and
   independently verified.
5. Before cleanup, classify each candidate from its own HEAD against its actual
   delivery ref using ancestry and `git cherry`. Close Syncwheel metadata before
   non-force worktree removal. If dirt, conflicts, or initialized submodules
   block removal, retain and report unless the exact deinit/force scope is
   separately approved; never delete remote branches under a local-cleanup gate.
