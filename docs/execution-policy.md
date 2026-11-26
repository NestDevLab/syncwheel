# Optional execution policy

`disabled`, `minimal`, `standard` and `strict` are operational private preferences. They never change
repository authority, forge requirements, ref ownership, Git locks, registry CAS,
publication leases, data preservation or destructive-operation proofs. They do
not authorize bypassing a required forge check or review.

Operational preferences discard clone-local historical author/merge-actor filters
and review-bypass permissions. The observed current principal still needs live
repository write permission and must satisfy published authority and forge rules.
An unavailable predecessor does not block an authorized writer. Legacy behavior
remains unchanged when no operational preference is selected.

| Gate | disabled | minimal | standard | strict |
| --- | --- | --- | --- | --- |
| Logical lane expiry | Off | Off | Off | Off |
| Logical writer exclusivity | Off | Warn | Warn | Warn |
| Optional CI | None | Required checks | All candidate checks | All candidate checks |
| Integration validation | Advisory | Advisory | Advisory | Required |
| Candidate test evidence | Off | Targeted | Targeted | Extended |
| Independent candidate review | Off | Sensitive | All changes | All changes |
| Installed package smoke | Off | Release completion | Release completion | Release completion |

All candidate checks means checks observed for the PR/composition's exact head,
not checks belonging to foreign stacks or prior revisions. Proven other-revision
checks are reported in `outOfScopeChecks`; an unproven revision remains blocking
under `all`. Required forge checks still need a successful matching candidate
observation, even when only an old revision has a check with the required name.
Strict integration validation checks this stack's declared integration commits;
it does not validate unrelated stack histories. Real dependency absorption and
published integration membership remain mandatory for every preset.

Unset preferences preserve legacy behavior. Pre-existing clone-local `checks` and `integration` values are private fallbacks,
not mandatory authority. An explicitly selected operational value replaces them.
Without opt-in, legacy merge behavior remains unchanged. Status reports each
field's preference provenance and any overridden legacy fallback; the merge plan
also reports live forge requirements and their mandatory provenance. Required
manifest integration membership and real dependency absorption still apply.

## Activation and inheritance

```sh
syncwheel self execution-policy set --preset minimal --apply --json
syncwheel repo execution-policy set --preset disabled --apply --json
syncwheel worktree execution-policy example-lane set --preset minimal --apply --json
syncwheel repo execution-policy status --json
syncwheel repo execution-policy clear preset --apply --json
syncwheel repo execution-policy clear --apply --json
```

Every mutation previews without `--apply`. Global settings work outside a Git
repository. Repository and lane settings use the ignored, untracked primary
`.syncwheel/profile.local.json`. Global, repository, then current lane values
resolve per field; a field override takes precedence over that level's preset.
`clear FIELD` restores the preset or parent value. Clear without fields removes
all execution preferences at that level and preserves unrelated data and evidence.

Individual options are `--expiry`, `--writer-exclusivity`, `--checks`,
`--integration-validation`, `--tests`, `--independent-review` and
`--installation-smoke`. Lane settings remain bound to the current stable lane
generation. Stale bindings inherit without reactivating old settings; explicit
`--bind-generation TOKEN` is required to rebind. No owner wait, renewal or lease
is introduced by any preset. Physical collisions still fail closed.

## Custom preferences

Custom means the existing per-field overrides; there is no `custom` preset or
new configuration level. An override can refine a preset at the same scope, or
inherit all unspecified fields from the parent. For example:

```sh
syncwheel self execution-policy set --preset strict --apply --json
syncwheel repo execution-policy set --checks required --tests targeted --apply --json
syncwheel worktree execution-policy example-lane set --independent-review sensitive --apply --json
syncwheel repo execution-policy clear checks --apply --json
```

Before the clear, CI and test preferences come from the repository, review from
the bound lane, and smoke/integration from global Strict. Clearing `checks`
restores global `all`; status exposes each field's scope and preset/override
provenance. Explicit overrides may select existing logical expiry/exclusivity
policies; no preset enables those blocking policies. These commands change only
the explicitly selected private settings, never another application implicitly.

## Existing candidate evidence

Syncwheel does not execute commands from configuration or create an approval
workflow. An operator first verifies existing test results and an independent
review, then explicitly records their original references. Records are
attestations of that verification, not proof that Syncwheel ran the tests or
performed the review. Never record unverified reports.

```sh
syncwheel repo execution-policy record --receipt tests.json --receipt review.json --apply --json
```

Each JSON receipt has `schema_version: 1`, the full `head_sha`, `kind`, `source`,
`reference`, `verified_by` and `outcome: "success"`. Use synthetic identities in
public examples; real receipts remain private. Additional fields depend on kind:

| Kind | Additional fields |
| --- | --- |
| `tests` | `scope`: `targeted` or `extended` |
| `review` | `scope`: `sensitive` or `all`; distinct `reviewer` and `author` |
| `installation_smoke` | `installed: true`; actual artifact `package_sha256` |

For example, a targeted test receipt is:

```json
{
  "schema_version": 1,
  "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "kind": "tests",
  "scope": "targeted",
  "source": "internal",
  "reference": "verified-test-run-reference",
  "verified_by": "operator",
  "outcome": "success"
}
```

Use `source: "ci"` for an existing CI test job and its exact check name in
`reference`. The merge planner also requires a live `SUCCESS` for that check on
the same candidate SHA; `SKIPPED`, a prior head or a missing observation does not
satisfy targeted tests. Internal test and review receipts retain their verified
references without imposing a new check name or a GitHub approver. Non-document changes and role/skill instruction files are conservatively sensitive. A conflicting receipt for the same head
and kind fails without overwriting the original. Private writes retain exact-byte
CAS and unrelated configuration.

The merge plan binds the effective preference provenance and consumed candidate
evidence into its existing digest. Required forge checks, changes requested,
unresolved review threads and mandatory integration membership retain their
existing enforcement. Preferences never enable a review bypass.

## Release completion

A successful merge candidate does not prove that its packaged release works.
After exercising the actual installed artifact, record its smoke receipt using
the release's verified source SHA and artifact SHA-256. Then run:

```sh
syncwheel repo execution-policy release-status --head FULL_SHA --json
```

Release status resolves the current checkout's generation-bound lane preferences
as well as global/repository values. For `minimal`, missing installed-package smoke reports `pending` and exits 1;
verified smoke reports `complete`. It does not block candidate merge or authorize
an installation. A different candidate SHA requires its own evidence. Recording
a receipt itself never runs or installs anything.
