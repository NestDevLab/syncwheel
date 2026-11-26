# Optional execution policy

`disabled` and `minimal` are operational private preferences. They never change
repository authority, forge requirements, ref ownership, Git locks, registry CAS,
publication leases, data preservation or destructive-operation proofs. They do
not authorize bypassing a required forge check or review.

| Gate | disabled | minimal |
| --- | --- | --- |
| Logical lane expiry | Off | Off |
| Logical writer exclusivity | Off | Warn |
| Optional CI | None | Required checks only |
| Integration validation | Advisory | Advisory |
| Candidate test evidence | Off | Targeted |
| Independent candidate review | Off | Sensitive changes |
| Installed package smoke | Off | Required for release completion |

Unset preferences preserve legacy behavior. Explicit clone-local merge policy
requirements remain a floor. The merge plan reports both the requested preference
and its effective mandatory floor; live forge requirements are always checked.

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
is introduced by either preset. Physical collisions still fail closed.

Only `disabled` and `minimal` are offered as CLI presets. The pure resolver's
other preset definitions are not an operational release promise.

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
references without imposing a new check name or a GitHub approver. Non-document
changes are conservatively sensitive. A conflicting receipt for the same head
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

For `minimal`, missing installed-package smoke reports `pending` and exits 1;
verified smoke reports `complete`. It does not block candidate merge or authorize
an installation. A different candidate SHA requires its own evidence. Recording
a receipt itself never runs or installs anything.
