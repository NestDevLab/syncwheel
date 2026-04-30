# Changelog

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
