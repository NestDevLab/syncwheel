# Cooperative test-slot qualification packet

Status: source candidate. Packaging and installed-slot qualification remain
separate evidence stages. The first implementation targets reviewed cooperative local jobs
on disposable repositories, without application services or a general sandbox.

## Reuse and boundaries

- Channel plans already pin base, ordered source revisions, composition digest,
  candidate tip and tree. Reuse that contract; do not rebuild shared integration.
- The existing unittest shard runner discovers tests and records durations. Reuse
  it for suites; it does not own process lifecycle or a physical test slot.
- `syncwheel_private_json.load_document/save_document` already provide snapshot
  CAS with an injected cooperative lock and atomic writer. Reuse those APIs for
  reservations and receipts instead of inventing a second configuration writer.
- The existing journal scheduler demonstrates user-unit integration but owns
  periodic publication, not test-job identity or recovery. Do not repurpose its
  timer or install another scheduler.
- Use the standard systemd user manager for transient job lifecycle. Python fits
  the existing CLI, manifest/channel APIs, CAS helper and stdlib fixtures; no new
  language runtime or dependency is needed.

Systemd 255 supports transient services, `Type=exec`, `ExitType=cgroup`, retained
terminal state and invocation identity. `ExitType=cgroup` keeps a service running
while any member remains; `RemainAfterExit=yes` preserves terminal observations
until explicit cleanup. See the versioned [service contract](https://www.freedesktop.org/software/systemd/man/255/systemd.service.html)
and [systemd-run contract](https://www.freedesktop.org/software/systemd/man/255/systemd-run.html).

## Requirements

1. A slot binds one canonical physical workspace, not a user-selected lock name.
   Repository/worktree administration identity and directory device/inode are
   checked before reservation and again before dispatch. Alias paths cannot make
   two slots for the same workspace. A record for a running or unknown job blocks
   reuse, independent of author availability, transcript or elapsed time.
2. Bind each operation to exact channel plan/composition digests, base/source
   revisions, candidate tip/tree, input and lockfile hashes, argv, environment
   allowlist, tool versions, log path and a fresh operation nonce.
3. Keep transaction locks short. Store a durable reservation before preparation;
   store `starting` before requesting the unit. Hold no manifest/publication lock
   while the job, its test or its review runs.
4. Start one nonce-named transient user service, with `Type=exec`,
   `ExitType=cgroup`, `KillMode=control-group`, a bounded runtime and retained
   terminal state. Do not use automatic collection before receipt persistence.
   Pass `--expand-environment=no` so the reviewed argv is not reinterpreted by
   systemd-run. Recheck the effective unit configuration and persist its
   `InvocationID`.
5. Give each job a separate HOME, temporary/output directories and reviewed
   environment, argv and working directory. Prepare inputs before dispatch and
   recheck the exact candidate and input hashes after the job. No shared mutable
   dependency store is introduced. Record the absence of applicable lockfiles.
6. This is cooperative isolation: jobs must not start external services, alter
   manager units or migrate work outside their unit. It is not a defense against
   hostile same-user code, host mutations or undeclared external services.
   A reviewed test command must collect its children and report their failures;
   a successful main exit alone cannot manufacture their test results.
7. Release only from a matching nonce, invocation and unit configuration with
   authoritative terminal observations. A running descendant retains the slot.
   Successful retained units can be `active/exited`; failed units can be
   `failed/failed`. Neither `ActiveState=active` alone nor `MainPID=0` proves that
   processes remain or have ceased. A retained signal termination can be a
   clean service result; record it as a negative test outcome, never success.
   Require a verified empty unit cgroup as
   well as the matching terminal service result and main exit. If the manager
   has retired the cgroup, qualify that observation explicitly; never interpret
   an empty cgroup path as the cgroup filesystem root or unknown as empty.
   Record unit result, main exit, candidate recheck, log digest, duration and
   available resource measurements before releasing. A failed job can release a
   physically idle slot but must retain its failed result.

## Crash and recovery contract

The reservation states are `reserved`, `starting`, `running`, `terminal` and
`unknown`. Receipt persistence and release use the existing JSON CAS contract.
The reviewed cooperative worker waits for its durable manager binding before
starting the selected command. This captures the invocation and nonempty unit
cgroup while the worker is alive, including for commands that would finish
before the coordinator's first observation. This is a standard user-service
startup handshake, not a custom kernel process or isolation primitive.

- A reserved operation can be cancelled by exact CAS before dispatch; a stale
  dispatcher must then refuse its transition to `starting`.
- A crash after `starting` never triggers an automatic second launch. Observe
  the nonce-named unit and validate its invocation, configuration and ownership.
  Missing or contradictory evidence remains unknown and retains the reservation.
- A coordinator crash while the unit or a descendant is active retains the slot.
  Recovery observes it; it does not kill another job or clear its reservation.
- A crash after terminal success but before receipt is reconciled from the same
  retained invocation. Append the missing receipt once, without running again.
- After receipt and release, stop/reset only this operation's retained unit.
  Preserve unowned files and unexplained residual state; cleanup is not inferred
  from a clean Git worktree.

### Retired cgroup evidence

The systemd 255 qualification observed `active/exited`, successful main exit,
`ControlGroup` empty, `TasksCurrent` not set and no pending manager `Job` after
the previously observed unit cgroup was removed. The invocation remained the
same; before that, a live child kept `running` even after `MainPID` became zero.

Accept retirement only when the reserved unit/nonce, recorded invocation and
effective configuration still match, the manager reports a stable terminal
outcome with no pending job, and the exact previously bound unit cgroup path is
absent. Do not read an empty path as the cgroup root. Missing identity,
configuration, earlier binding or terminal evidence remains unknown; absence of
a path, `TasksCurrent` or main PID alone never releases the slot. Fixtures must
reject each missing witness and changed invocation separately.

## Disposable qualification

| Proof | Required observation |
|---|---|
| Two compositions | Different pinned candidates run consecutively; the second cannot see the first job's HOME, temporary or output sentinel. Both receipts identify their exact tree. |
| Contention and aliases | A second claimant of the same physical slot refuses, including through a path alias. An unrelated composition can still plan/publish while the test is active. |
| No long transaction lock | Independent processes can acquire the real manifest/publication transaction locks while the cooperative job waits; slot reuse still refuses. |
| Coordinator crash | Kill only the fixture coordinator; its own service/child remains observed and the reservation stays occupied until terminal state is proven. |
| Lost terminal receipt | Interrupt after a successful retained unit outcome; reconcile one receipt without a second dispatch or changed result. |
| Candidate/input drift | Different head, tree, lockfile or unit invocation refuses qualification and preserves evidence. |
| Exact argv | Literal dollar signs and variable-looking argv are unchanged; configuration or argv mismatch refuses binding. |
| Retained terminal state | Accept verified empty `active/exited` success and verified empty failed outcomes; refuse `MainPID=0` while a child remains and refuse unknown process observations. |
| Release | After child cessation, exact receipt and release succeed once; duplicate release is idempotent and cannot release a newer reservation. |
| Measurements | Record wall/test/queue time separately, CPU/memory where available, fixture disk delta and an explicit cleanup disposition. |

Before implementation, independently review the reservation/dispatcher boundary
and the retained-unit observations. Real qualification uses only nonce-owned
fixture units and disposable paths; no application configuration is selected.

## Prototype boundary

The current source prototype separates the reservation domain
(`syncwheel_test_slot`) from its native adapter
(`syncwheel_test_slot_native`). The native adapter chooses the store through
Git's canonical worktree administration directory, so aliases cannot choose a
second reservation file. It verifies physical workspace and administration
identity, and the worker rechecks the selected inputs after the durable startup
handshake. Uncertain terminal observations stop recovery without releasing.

The request contains the existing `channel_plan` object, a `manifest_blob`
Git object id, literal `argv`, an explicit `environment`, and optional
`observations`. Capture the control manifest as an immutable Git blob in the
slot repository's object store; no commit or shared integration rebuild is
required for that capture. The adapter verifies the object bytes, normalizes
with the canonical manifest contract, checks the plan digest and channel
pin/dependency/resolution identities, and verifies the selected Git history.

The worker starts only after its InvocationID and live cgroup are durably
bound. `Restart=no` prevents implicit redispatch. It rechecks workspace/admin/
index identity and its observed tools, then uses the existing ReplayEngine
projection inside the same unit before executing the selected test command.
A discordant tree fails qualification before the test command; all preparatory
Git children remain owned by the unit. CPU and memory accounting are explicit.

Caller observations and the plan's moving-ref observations remain reported,
not authenticated facts about a forge, reviewer or source author. The observed
tool inventory covers the controller modules/tools and command entrypoint;
payload descendants remain caller-controlled cooperative code. Neither the
slot receipt nor a successful materialization manufactures a CI, review,
landing or installation approval.

Use a clean, detached candidate worktree whose local channel ref identifies the
candidate. Ignored/untracked residue is not silently reused. Job HOME, XDG,
temporary and output roots are private; direct outputs to
`SYNCWHEEL_TEST_OUTPUT`. Controlled bytecode/read-only Git settings prevent
incidental candidate/index writes. A changed input or residue is reported,
never silently reset or deleted.

```sh
syncwheel-test-slot status "$slot_workspace"
syncwheel-test-slot inspect "$slot_workspace" request.json
syncwheel-test-slot run "$slot_workspace" request.json --timeout 300
syncwheel-test-slot recover "$slot_workspace" RECORDED_NONCE
syncwheel-test-slot cleanup "$slot_workspace" RECORDED_NONCE
```

`status` reads the canonical active operation and retained receipts, including
the nonce needed after a lost coordinator reply. Reconcile that record before
requesting another run. `inspect` derives verified request fields without dispatch. `run` records the
reservation before starting a nonce-owned unit. `recover` observes that same
unit, never launches another. Receipt/release is one CAS operation after
verified physical termination; failed qualification can release a physically
idle slot while retaining its failure. `cleanup` requires that committed
receipt and matching unit configuration. Missing native process evidence
remains unknown and occupied. Prepared inputs, logs and unexplained residue
are retained for explicit disposition. Aggregate resource-budget enforcement
and controlled-pilot adoption are separate qualifications.
