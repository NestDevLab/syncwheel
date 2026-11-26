# Preference resolution contract

The pure `syncwheel_policy` module defines one resolution table. The existing
worktree preference API delegates its two supported fields to that resolver and
keeps its existing JSON shape, CLI choices and implicit defaults.

The public [execution policy](../execution-policy.md) activates `disabled` and
`minimal` through global, repository and generation-bound lane scopes. The older
worktree API remains a two-field projection with its original JSON shape and
field choices. Both consumers share the same resolution table and private
configuration. The `standard` and `strict` definitions remain an internal
contract, not an operational CLI promise. This pure module does not persist
settings, discover a lane or migrate configuration.

## Explicit presets

| Field | disabled | minimal | standard | strict |
| --- | --- | --- | --- | --- |
| expiry | off | off | enforced | enforced |
| writer_exclusivity | off | warn | block | block |
| checks | none | required | all | all |
| integration_validation | advisory | advisory | advisory | required |
| tests | off | targeted | targeted | extended |
| independent_review | off | sensitive | required | required |
| installation_smoke | off | required | required | required |

The table describes optional requirements, not evidence that a check ran.
`checks=none` cannot waive required forge checks. A review requirement does not
create a review receipt. Smoke applies when installing, not on every command.
Candidate identity, integrity, CAS, real ownership, authority and forge rules
remain mandatory under every preset; they are not preference fields.

With no explicit preference, expiry remains `enforced`, writer admission remains
`legacy`, and delivery fields remain `legacy`. Consumers must retain their
existing behavior for those unspecified fields. They must not select `standard`
implicitly during an upgrade.

## Resolution and provenance

Resolution order is built-in defaults, user, repository, then a caller-selected
lane document. Each level expands its preset first, then applies that level's
explicit field overrides. An empty level inherits. Removing a field restores its
preset value if a preset remains at that level; removing the preset leaves the
other explicit overrides intact. `custom` means these field overrides, not an
additional preset or configuration system.

The result includes effective values, requested values, per-field scope and
whether a value came from a preset, an explicit override or the mandatory floor.
Input documents are not mutated. Lane identity and generation validation remain
the caller's responsibility: this pure resolver neither discovers lanes nor
uses process identity to choose one.

A consumer can supply a mandatory field floor from an independently verified
canonical policy or authority source. Resolution may strengthen a requested
field to this minimum and reports both the original request and its provenance.
A private preference cannot supply or replace this floor. Invalid fields,
presets, values and floor entries fail closed. A floor never reduces a stronger
request. In addition to this field floor, consumers must retain their mandatory
Git, authority, ownership, multiwriter and forge checks independently.

## Consumer qualification

Operational consumers must bind the lane by
stable identity and generation, obtain the floor from the governing source, and
wire each supported optional field to its actual gate or documented procedure.
Prove inheritance and clearing, required-rule enforcement, and that targeted
publication advances its selected stacks while preserving unrelated worktree
bytes, refs, index, registry and locks through interruption and retry. A profile
resolver alone does not prove that publication or recovery behavior.
