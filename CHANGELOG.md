# Changelog

## 0.18.0 - 2026-06-10

- Add uv packaging with a `syncwheel` console script while preserving direct
  `python3 scripts/syncwheel.py ...` execution.
- Add an idempotent `scripts/install.sh` for production uv installs and
  editable development installs.
- Extend `self status`, `self check-update`, and `self update` to distinguish
  git checkouts, uv tool installs, and plain script execution.
- Teach uv tool installs to check the upstream `VERSION` file directly and
  update with uv.
- Add CI coverage for editable and git-sourced uv tool install modes.

## 0.17.0 - 2026-05-13

- Add a segmented append-only ledger under `.syncwheel/ledger/` with a replayed
  checkpoint for cross-machine recovery state.
- Record manifest saves, stack rebuilds/pushes, and integration rebuilds/
  alignments/pushes into the ledger.
- Add `ledger show` to inspect the current replayed ledger state.
- Teach `resume` to restore previously known historical stacks from the ledger
  when ownership is deterministic and the historical branch still exists.

## 0.16.1 - 2026-05-13

- Remove Jira-specific stack auto-creation from `resume` so the recovery flow
  stays tracker-agnostic.
- Keep `resume` conservative: it now auto-registers only commits with exactly
  one already-detected owner and leaves all other cases in manual review.

## 0.16.0 - 2026-05-13

- Add `reconcile --mode resume` and the top-level `resume` command for
  cross-device recovery flows.
- Let `resume` auto-register unmapped integration commits on a deterministic
  owning stack.
- Allow integration rebuild/alignment to proceed from the primary checkout when
  that checkout is dirty only because of untracked `.syncwheel/` metadata.

## 0.15.0 - 2026-05-07

- Add commit-level guidance for unmapped integration commits in `check` and
  `reconcile` output.
- Include changed files, containing branches, likely stack owners, related
  declared commits with matching subjects, and suggested next commands.
- Add JSON diagnostics under `diagnostics.unmapped_integration_commits` for
  automation and tests.

## 0.14.0 - 2026-05-05

- Add top-level `sync` and `publish` lifecycle commands.
- Make safe local-to-remote alignment the default for `reconcile --apply`,
  `sync`, and `publish` when local and remote both match the manifest
  projection, with `--no-align-local-to-remote` as the escape hatch.
- Improve reconcile plan wording for remote projection alignment, local
  projection publishing, unassigned integration commits, and manual review
  cases.

## 0.13.2 - 2026-05-05

- Make `stack add` validate integration-first commits immediately.
- Reject commits made on top of a stale integration projection before mutating
  the manifest, and validate the updated stack projection before saving.

## 0.13.1 - 2026-05-05

- Stop writing the fallback `Syncwheel <syncwheel@example.com>` identity into
  target repository Git config during projection worktrees.
- Respect normal Git identity resolution for commit-creating commands and emit
  a yellow warning before using the Syncwheel fallback identity only when
  `user.name` or `user.email` is missing.

## 0.13.0 - 2026-05-05

- Add `stack absorb` for integration-first workflows where changes are made on
  the integration branch and then moved into the owning stack branch.
- Support pathspecs, `--staged`, default amend behavior, `--no-amend` with a
  custom commit message, and worktree creation/reuse for the target stack.
- After a successful absorb, update the manifest commit list and remove the
  absorbed patch from the integration checkout.

## 0.12.1 - 2026-05-05

- Show `git status --short --branch` in `reconcile` output before validation
  and drift sections so dirty working trees are explicit.
- Include `working_tree_status` and `working_tree_dirty` in `reconcile --json`
  output.

## 0.12.0 - 2026-05-04

- Add `reconcile --align-local-to-remote` for history normalization when local
  and remote branches both match the manifest projection but still differ by Git
  history.
- Keep that normalization explicit so normal `reconcile` remains a content
  no-op in the both-valid case.
- Add regression coverage for stack and integration branches with diverged
  history and identical projected trees.

## 0.11.1 - 2026-05-04

- Fix `reconcile` no-op detection when rewritten local and remote histories
  already match the manifest projection by tree but do not contain the exact
  historical manifest SHAs.
- Prevent validation SHA-containment drift from forcing rebuilds when
  `local_matches_projection` is already true.
- Add a regression test for diverged commit history with the same projected
  tree.

## 0.11.0 - 2026-05-04

- Make `reconcile` converge stale local managed branches to remote refs when
  those remote refs already match the manifest projection.
- Avoid regenerating new replacement SHAs, updating the manifest, or pushing
  again in the normal multi-device case where another device has already
  published the correct projection.
- For `merge-stacks` integration projections, let `reconcile` evaluate remote
  stack refs that already match the manifest so stale local stack branches do
  not cause false integration rebuilds.

## 0.10.0 - 2026-05-04

- Make `reconcile --push` use `--force-with-lease` by default, matching the
  normal multi-device lifecycle for rebuilt managed branches.
- Add `reconcile --no-force-with-lease` as the explicit escape hatch for normal
  Git pushes.

## 0.9.1 - 2026-05-04

- Add explicit `--force-with-lease` support to `reconcile --push`, `stack push`,
  and `int push` so the common rewritten-branch publish path does not require
  remembering Git passthrough syntax.
- Keep Git passthrough after `--` available for advanced push flags.

## 0.9.0 - 2026-05-04

- Add top-level `reconcile` / `rec` as the preferred multi-device maintenance
  workflow for manifest-owned stacks and integration branches.
- Report stack and integration drift against local branches, remote refs, and
  manifest-projected trees.
- Support dry-run-by-default planning, explicit `--apply`, optional `--push`,
  worktree-root rebuilds, stack filtering, publication remote override, and
  manifest SHA refresh after stack rebuilds.
- Add tests for reconcile planning and apply behavior with an external
  manifest.

## 0.8.2 - 2026-05-04

- Add `self install-hooks` so any Syncwheel clone can install the tracked Git
  hooks with a standard Syncwheel command.
- Report hook activation state in `self status`.

## 0.8.1 - 2026-05-04

- Add a tracked pre-commit hook for the version-bump guard.
- Add staged-file mode to `scripts/check-version-bump.py` so local hooks can
  reject commits before they are created.

## 0.8.0 - 2026-05-04

- Add `int sync-status` to compare local integration, remote integration, and
  the manifest-projected integration tree.
- Add `int align-remote` to backup and reset a clean shared integration checkout
  to its remote only when the remote matches the manifest projection, unless
  explicitly forced.
- Add `manifest compare` to inspect different integration compositions and
  identify shared, divergent, and composition-only stacks.
- Add end-to-end Git tests covering shared-integration remote alignment and
  multi-manifest comparison.
- Add a version-bump guard so release-relevant CLI changes must update
  `VERSION`, `CHANGELOG.md`, and the README current-version line.

## 0.7.2 - 2026-05-02

- Publish the detached-head update-detection fix as a new release version so pinned installs can verify the notifier behavior against a newer tagged version.

## 0.7.1 - 2026-05-02

- Detect available updates for detached-head and submodule-style syncwheel installs.
- Reuse existing target worktrees more safely during rebuilds.
- Clarify detached-install update detection in the docs.

## 0.7.0 - 2026-05-02

- Add built-in self update commands: `self status`, `self check-update`, and
  `self update`.
- Add automatic per-install update policy with `self mode off|notify|auto`.
- Emit visible update notices on normal syncwheel usage so human operators and
  AI agents do not silently keep using an outdated checkout.

## 0.6.0 - 2026-04-30

- Make `main-integration` the default shared integration branch created by
  `init`.
- Update the documented default operating model so day-to-day combined work
  happens on the integration branch and `main` remains the promotion branch.

## 0.5.1 - 2026-04-30

- Document `init` as the default manifest bootstrap command; keep `--stdout`
  as an advanced piping option.

## 0.5.0 - 2026-04-30

- Add repo-local profile selection with `use <profile>` and `use --shared`.
- Resolve `.syncwheel/profile.local.json` automatically when no explicit
  manifest or personal profile is passed.

## 0.4.0 - 2026-04-30

- Add `check`/`ck` as a single fetch + validate + plan command for the common
  inspection flow.
- Add short aliases for common commands (`st`, `v`, `pl`, `s`, `i`, `s new`,
  `s rb`, `i rb`, `g`) and `-p` for personal manifests.
- Add `SYNCWHEEL_REPO` and `SYNCWHEEL_PERSONAL` environment defaults so host
  projects can provide concise wrapper commands.

## 0.3.0 - 2026-04-30

- Add `init --personal <name>` to create ignored local manifests under
  `.syncwheel/manifests/<name>.local.json`.
- Add `stack create` so stack entries can be created without hand-editing the
  manifest.
- Document command-first manifest and stack creation flows for humans and AI
  agents.

## 0.2.0 - 2026-04-30

- Replace the previous materialization UI with the object/action CLI:
  `stack ...` and `int ...`.
- Add `stack sync`, `stack set`, and `stack add` so commit lists do not need to
  be edited by hand.
- Add `stack rebuild` and `int rebuild`, with worktree mode, `--in-place`, and
  `--dry-run`.
- Add `stack push` and `int push` wrappers around `git push`, including
  passthrough arguments after `--`.
- Add `stack git` and `int git` wrappers for running arbitrary Git commands in
  the target branch worktree.
- Add `integration.strategy: "merge-stacks"` for merge-shaped integration
  branches.
- Create automatic backup branches before rebuilding existing targets.
- Document the worktree-first model and human command recipes.

## 0.1.0 - 2026-04-29

- Initial manifest-driven status, validation, plan, and deterministic branch
  rebuild workflow.
