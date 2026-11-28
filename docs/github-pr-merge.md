# GitHub PR merge policy

Syncwheel can plan and apply a GitHub pull-request merge through a narrow,
clone-local policy. The selected manifest owns the stack and its effective
authority must be `ai-managed` with `source_change` allowed. Lane and personal
manifests inherit the primary shared authority when they have no declaration;
an explicit declaration may restrict but cannot widen it. Merge plans bind that
authority and recheck it before merging. The policy itself
is stored in `.syncwheel/profile.local.json`, which must remain ignored and
untracked.

## Review evidence and formal-review gates

Use the [agent review procedure](ai-agents.md#independent-review-and-merge) to
record an independent technical verdict at the exact head. A truthful same-account
GitHub `COMMENT` records that result; it does not satisfy a distinct-identity
approval rule. Never invent an identity or change governance implicitly.

A private `allowed_bypasses: [required_reviews]` policy is usable only after explicit
authorization for the repository, permitted bases, merge actor and source provenance.
Inspect authority and `repo pr-merge-policy status --json` before selecting it;
configuration is not itself authorization. The operator must verify independent technical READY and green CI externally
at the exact SHA; a legacy policy may allow `checks: "none"`, so planner readiness
alone does not prove those conditions. The plan must have no other blockers and
select only the proven formal-review path. The planner does not ingest or
authenticate a subagent's verdict. Changes requested, unresolved threads, conflicts, stale bases,
merge queues and unknown observations still block. Explicit human-gated governance
remains binding. Operational execution preferences currently clear these legacy
bypasses: `disabled` or `minimal` does not enable this exception.

The fixed adapter emits the equivalent of this command only for the reviewed,
preauthorized admin-review path (shown with a squash policy):

```sh
gh pr merge "$PR" --repo "$REPO" --squash --admin --match-head-commit "$REVIEWED_SHA"
```

`--admin` is a broad GitHub permission, not a review-only capability; it can bypass
other branch requirements server-side. It is non-default, grants no permissions
and is safe here only within the proven, authorized planner scope. For managed
work, use the digest-bound `stack merge-pr` plan/apply below rather than copying
this command after a refusal. The adapter never adds `--delete-branch`.
For already authorized native maintenance, the same explicit scope and evidence
are required before choosing this command; the maintenance exception alone does
not authorize it. Preserve audit, exact-head merge readback and ordinary
reconciliation without a second merge after an uncertain response.

## Configure the private policy

The command is dry-run by default and preserves unrelated profile keys:

```bash
syncwheel repo pr-merge-policy set github \
  --repository OWNER/REPO \
  --base main \
  --method squash \
  --merge-actor LOGIN \
  --pr-author LOGIN \
  --commit-author LOGIN \
  --head-repository OWNER/REPO \
  --checks all
```

The example authorizes no bypasses. Add `--allow-bypass required_reviews` only
when an administrator review bypass is explicitly authorized; an unmet review
otherwise blocks the merge. `--allow-bypass private_free_rules` is independent
and must also be explicitly configured.

Review the JSON preview, then repeat with `--apply`. Inspect or remove the
policy with:

```bash
syncwheel repo pr-merge-policy status --json
syncwheel repo pr-merge-policy clear --apply
```

`--base` is repeatable. Each value is an exact branch name, or a `PREFIX/*`
pattern such as `save/*` or `release/*` that allows every branch under that
prefix, so release branches do not have to be listed one by one. A bare `*` and
other glob characters are rejected.

At least one provenance filter (`--pr-author`, `--commit-author`, or
`--head-repository`) is mandatory. Configured filters are combined with AND;
empty lists, unknown keys, tokens, commands, and secrets are rejected.

`--checks all` is the fail-closed default: at least one check must exist and
every current check must finish successfully or be skipped. For the exact PR
head, Syncwheel groups check runs by check name and verified workflow identity
and evaluates the latest run in each group. A rerun attempt is compared only
within its own run. Superseded runs are reported as informational warnings; a
latest queued, cancelled or failed run still blocks. Ambiguous identity or
ordering never hides a blocking observation. Incomplete or truncated check
history blocks the merge even when the rows received so far are green. Legacy status contexts remain
separate from workflow checks. The same selection applies to required checks,
so success in an unrelated workflow with the same check name cannot hide a
failure.

A repository without CI may explicitly use `--checks none`; the merge plan
then records a `checks_not_required` warning. This opt-out is clone-local and
does not bypass required checks reported by GitHub branch protection or rulesets.

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
review and rules checks must pass. By default every commit the stack owns in
the integration projection must be on the integration branch
(`integration_not_validated` otherwise). A clone whose integration is often
behind can set `--integration advisory` on the policy: missing commits or a
missing local integration ref become a warning, and the PR is validated by its
own checks, pinned head and live base only. The plan also reports a primary
staging-layout mismatch as advisory; it never creates a local projection ref to
satisfy this check. Global validation and integration operations retain their
primary-layout requirements. Declared membership, published source identity,
real dependencies, authority and forge-required checks still block in both modes.
CI must also pass unless the clone-local policy explicitly declares
`checks: "none"`:

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

### Scenario authority observation

The fixed adapter also exposes the read-only `observe_acceptance_authority`
operation. It reads the destination through authenticated `github.com` APIs and
returns its numeric repository ID, exact live base ref, commit/tree identities
and the policy blob (or complete-tree proof of absence). Local Git aliases and
owner/repository path similarity are not authority evidence. Host, repository ID,
branch, base and policy content must agree with the merge plan, and apply reads
them again before mutation. Unavailable verification blocks distinctly from
verified policy absence. This adds no human-signoff requirement when that policy
is absent. See [scenario acceptance](scenario-acceptance.md) for activation and
source verification boundaries.
