# Syncwheel post-delivery housekeeping

Status: implemented. This document records the supported workflow and its
safety boundary. The earlier proposal for `--merged-by-content` was superseded
by the stronger `stack close --reason absorbed` proof.

## Close proof

Use the proof that matches the delivery method:

```bash
syncwheel stack close <id> --reason merged    # declared commits reached the target
syncwheel stack close <id> --reason absorbed  # squash/rebase final content reached the target
```

`merged` requires every declared stack commit to be reachable from the target
branch. `absorbed` fetches the current delivery tip and verifies the fully
composed stack result across every touched path. It accepts equivalent squash or
rebase output and rejects incomplete delivery or a later revert. Neither normal
path needs `--force`.

`--force` remains an explicit escape hatch for closing without ancestry or
absorption proof. It is not the squash-merge workflow.

## Local cleanup

Closing shared stack state and reaping local resources are separate operations.
After a proved close:

```bash
syncwheel validate
syncwheel gc
syncwheel gc --apply
```

`gc` plans first, preserves dirty or conflicted worktrees, and removes only
resources the plan proves safe to reap. Another machine cleans its own local
worktrees; shared manifest and ledger state remain the coordination contract.

## Agent rule

Before the first use of a mutating subcommand in a session, and after an upgrade
or syntax error, run its exact nested help, such as:

```bash
syncwheel stack close --help
syncwheel gc --help
```

The installed help is authoritative for that version's flags and defaults. If
it disagrees with repository documentation, verify the installed version and
source documentation before changing repository state.

## Acceptance coverage

- ancestry delivery closes with `--reason merged`;
- equivalent squash and composed squash delivery close with `--reason absorbed`
  and no force flag;
- incomplete delivery and reverted content are rejected;
- dirty or conflicted worktrees remain preserved during cleanup;
- repeated cleanup converges to a no-op.
