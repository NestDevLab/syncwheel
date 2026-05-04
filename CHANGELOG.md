# Changelog

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
