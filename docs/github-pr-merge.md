# GitHub PR merge policy

Syncwheel can plan and apply a GitHub pull-request merge through a narrow,
clone-local policy. The shared manifest still owns the stack and must declare
`authority.mode: "ai-managed"` with `source_change` allowed. The policy itself
is stored in `.syncwheel/profile.local.json`, which must remain ignored and
untracked.

## Configure the private policy

The command is dry-run by default and preserves unrelated profile keys:

```bash
syncwheel repo pr-merge-policy set github \
  --repository OWNER/REPO \
  --base main \
  --method squash \
  --allow-bypass required_reviews \
  --merge-actor LOGIN \
  --pr-author LOGIN \
  --commit-author LOGIN \
  --head-repository OWNER/REPO
```

Review the JSON preview, then repeat with `--apply`. Inspect or remove the
policy with:

```bash
syncwheel repo pr-merge-policy status --json
syncwheel repo pr-merge-policy clear --apply
```

At least one provenance filter (`--pr-author`, `--commit-author`, or
`--head-repository`) is mandatory. Configured filters are combined with AND;
empty lists, unknown keys, tokens, commands, and secrets are rejected.

## Plan and apply

The stack must be `published`, its remote branch must exactly match the
declared commit projection, and all local, integration, identity, repository,
review, rules, and CI checks must pass:

```bash
syncwheel stack merge-pr STACK --json
```

The command emits a `githubPrMergePlan` only. Applying requires both values
from that exact plan:

```bash
syncwheel stack merge-pr STACK \
  --operation-id OPERATION_ID \
  --plan-digest PLAN_DIGEST \
  --apply
```

The fixed adapter invokes only `gh pr merge`, pins the operation with
`--match-head-commit`, and never deletes the remote branch. `--admin` is added
only when GitHub proves that required reviews are the sole remaining blocker.
CI failures, conflicts, stale bases, changes requested, unresolved threads,
merge queues, unknown rules, and unrecognized states always stop the plan.

If the command or connection fails after preparation, repeating the same
operation id and digest reconciles the remote PR without issuing a second
merge. Receipts distinguish `succeeded`, `succeeded-equivalent`, `failed`, and
`unknown` and are recorded in the local Syncwheel ledger.
