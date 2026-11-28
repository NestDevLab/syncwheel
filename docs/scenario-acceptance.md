# Scenario acceptance

`repo acceptance` separates a person's scenario decision from an agent's
candidate-delta assessment. It does not run tests, approve a pull request, grant
repository authority or authorize deployment. Tests and independent review retain
their own contracts.

## Provenance and exact applicability

A receipt contains schema version 1, kind `scenario_acceptance`, `scenario_id`,
concrete `scenario` text, the declared `person`, an `accepted_on` date
(`YYYY-MM-DD`), an exact `binding` and a `source` locator. Bindings are either:

```json
{"revisions": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]}
```

or an ordered composition:

```json
{
  "composition": {
    "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "head_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "stacks": [
      {"id": "producer", "head_sha": "cccccccccccccccccccccccccccccccccccccccc"},
      {"id": "consumer", "head_sha": "dddddddddddddddddddddddddddddddddddddddd"}
    ]
  }
}
```

`head_sha` identifies the composition result, not its last input. Order, base,
result and every stack head are part of the binding. The command checks that
revisions exist as commits; it does not build a composition or prove that its
inputs replay to its declared result. The composition owner retains that proof.

Record a portable receipt without claiming its person was authenticated:

```sh
syncwheel repo acceptance record --receipt acceptance.json --json
syncwheel repo acceptance record --receipt acceptance.json --apply --json
```

For a source without an implemented trusted owner, use
`{"kind":"provenance","reference":"decision-record-17"}`. Its status stays
`source_unverified`. A supplied `human_verified`, `approved` or equivalent field
is not accepted. No receipt or agent assessment can populate the trust policy.

## Live GitHub source adapter

A source locator contains:

```json
{"kind":"github_review","repository":"example/project","pull_request":7,"review_id":17}
```

The review body must be the receipt's JSON payload **without** `source`.
The source locator supplies that field during recovery. The observed review must
belong to the exact repository, pull request and stable review ID. Its author
must match `person`, its submission date must match `accepted_on`, and its
`commit_id` must belong to `revisions` or equal the composition's result head.

The fixed `syncwheel_github` adapter reads the review, the authenticated operator
and the author's live repository permission through `gh api`. It verifies an
`APPROVED` review, a `User` author whose stable numeric ID is explicitly trusted,
current write/maintain/admin permission for that same ID, and an author distinct
from the authenticated operator. `User` account type or permission alone does
not prove a human. If the operator and approver are indistinguishable, the
source remains unverified; do not invent another identity to obtain a positive
status. A different trusted source owner can be added as a separate adapter.

Only the maintainer provisions trust in the existing user settings document
(`~/.config/syncwheel/settings.json` by default), through the repository or
installation owner's approved configuration procedure:

```json
{"acceptance_trust":{"github":{"example/project":[100]}}}
```

This is a trust-policy decision, not ordinary candidate data. The commands in
this family never edit it and ignore trust declarations inside candidate files
or the repository's receipt cache. Do not configure trust in a consumer project
merely because a receipt requests it. User settings and the executing adapter
are trusted inputs: the local CLI is not a sandbox against a process that can
rewrite its executable, credentials or user settings. Protected policy delivery
belongs to the host/installation owner.

## Recovery, assessment and renewal

Recover directly from the shared source, including in a new clone:

```sh
syncwheel repo acceptance status --source source.json --head FULL_SHA --json
```

The forge review is the shared source of the decision. The ignored, untracked
repository profile is only a portable-receipt/assessment cache. Its updates use
the existing exact-byte private-configuration CAS; a concurrent writer causes a
refusal. A new clone needs the source locator and independently provisioned trust,
not the previous clone's profile. Every applicability check reads the source
again. A changed body requires recovering the new receipt; a dismissed review,
revoked permission or lost trust cannot continue to satisfy applicability.

With `--receipt acceptance.json`, or `--acceptance-id DIGEST` after recording,
the default candidate is the invoking checkout's exact HEAD. Use `--candidate
composition.json` for an explicit ordered composition instead of `--head`.
An exact binding needs no delta assessment. A changed candidate requires the
agent to assess the impact on the accepted scenario:

```sh
syncwheel repo acceptance assess --receipt acceptance.json --head FULL_SHA \
  --impact unchanged --rationale "Only diagnostic wording changed." \
  --reference delta-review-17 --apply --json
```

`changed` or `uncertain` requires renewed human acceptance. `unchanged` preserves
applicability only with a currently verified source and an assessment bound to
the exact receipt digest and candidate. A new SHA, reordered composition or
changed base cannot inherit another candidate's assessment. The command records
the current agent actor, rationale, reference, timestamp and observed file delta
where a single before/after head is defined. File names alone do not establish
semantic impact: `unchanged` remains the agent's accountable judgment, not an
automatic test result or a human signature. Applied assessments retain history.
`status --assessment assessment.json` consumes an external raw assessment with
the same exact bindings; supplied author/approval fields are rejected.

Successful applicability returns exit 0. `renewal_required` and
`source_unverified` return exit 1. Invalid input, mismatched source identity,
changed source content, unavailable source or configuration CAS failure returns
exit 2. `record` returns 0 for successful provenance storage even when no human
source is verified. No command changes product refs, manifest, worktree registry,
forge data or deployment state.

The positive automated smoke uses controlled fixture identities and a read-only
fixture `gh` executable. It proves command wiring and trust checks, not the
identity of any real human or activation in a real repository.
