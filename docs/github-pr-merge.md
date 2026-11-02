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
  --allow-bypass private_free_rules \
  --merge-actor LOGIN \
  --pr-author LOGIN \
  --commit-author LOGIN \
  --head-repository OWNER/REPO \
  --checks all
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

`--checks all` is the fail-closed default: at least one check must exist and
every observed check must finish successfully or be skipped. A repository
without CI may explicitly use `--checks none`; the merge plan then records a
`checks_not_required` warning. This opt-out is clone-local and does not bypass
required checks reported by GitHub branch protection or rulesets.

`private_free_rules` is a narrow fallback for private repositories on GitHub
Free. It applies only when GitHub returns its explicit plan-limit response for
the branch-protection or rules APIs and the repository is confirmed private.
Syncwheel still requires an admin actor, an exact allowlisted source, a pinned
head, green CI, no unresolved review work, and a mergeable PR. The final
`gh pr merge` remains subject to GitHub's server-side enforcement. Other 403s,
public repositories, and unrecognized errors remain blocked.

## Alternative observation of complete absence

The adapter can observe empty rules through GraphQL when a private repository's
REST branch-protection or branch-rules endpoint returns HTTP 403 with the exact
message `Upgrade to GitHub Pro or make this repository public to enable this feature.`
This is alternative evidence, independent of the `private_free_rules` bypass.
It does not assume that an unavailable REST endpoint means no rules exist.

Both REST and GraphQL must identify the requested repository as private and the
actor as admin. The static GraphQL query uses owner/name variables and requests
`branchProtectionRules(first:100)` plus
`rulesets(first:100,includeParents:true)`. Both connections must report integer
`totalCount: 0`, empty `nodes`, and boolean `hasNextPage: false`. Any GraphQL
errors, missing fields, nonempty or paginated connections, identity mismatch,
permission denial, or incompatible REST observation leaves the original REST
failure blocking. Existing nonempty REST observations are never overwritten.

Successful proof sets `rules.observationSource` to `graphql-complete-empty` and
records the returned repository and complete connections in `emptyRulesEvidence`.
The normalized empty rule values have observation status 200, while
`restObservation` preserves the actual REST statuses, values and errors; it does
not claim that the REST requests succeeded. Without proof the source stays
`rest`. The core merge gates remain unchanged: `rulesFallback` stays null, and
CI, reviews, provenance and pinned-head checks still apply. This proof does not
request an admin merge.

## Plan and apply

The stack must be `published`, its remote branch must exactly match the
declared commit projection, and all local, integration, identity, repository,
review and rules checks must pass. CI must also pass unless the clone-local
policy explicitly declares `checks: "none"`:

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
Unavailable rule APIs stop the plan unless complete alternative observation
proves absence as described above, or the explicit `private_free_rules` policy
covers the exact GitHub Free private-repo response.

If the command or connection fails after preparation, repeating the same
operation id and digest reconciles the remote PR without issuing a second
merge. Receipts distinguish `succeeded`, `succeeded-equivalent`, `failed`, and
`unknown` and are recorded in the local Syncwheel ledger.
