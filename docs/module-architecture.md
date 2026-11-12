# Source module boundaries

`syncwheel.py` remains the CLI and application composition root. The supported
CLI, `main` import and revision-provider entry points remain available. Direct execution,
installed entry points, and loading it with `importlib.util.spec_from_file_location`
resolve the same sibling modules. Runtime version and source checkout detection
remain in that facade, where `__file__` still identifies the entry script.

| Module | Responsibility | Dependencies |
| --- | --- | --- |
| `syncwheel_manifest_store` | Manifest transactions, lifecycle ledger, durable control commits and crash recovery | Shared transaction state; explicit repository, projection, workspace tracking and lifecycle capabilities |
| `syncwheel_manifest` | Manifest normalization, authority, stack dependencies, channel schema | Shared errors; standard library |
| `syncwheel_coordination` | Snapshot validation, portable refs, proposal comparison and composition | Manifest domain; explicit branch validator |
| `syncwheel_coordination_state` | Immutable remote state, control-manifest integrity and exclusive ownership evidence | Git observations, manifest inputs and state witnesses |
| `syncwheel_publication_intents` | Intent identity, durable token lifecycle and bounded landing proofs | Git, ledger, state reader and transaction/evidence ports |
| `syncwheel_publication` | Atomic publication, source leases, snapshots and reconciliation | Composed state, intent and replay services; Git/storage/projection/ledger ports |
| `syncwheel_journal` | File admission, status decoding, safe content observation | Shared errors; explicit status and reader inputs |
| `syncwheel_journal_workflow` | Snapshot, publish, pull and recovery sequencing | Journal policy; explicit repository I/O port |
| `syncwheel_ledger` | Event reduction into lifecycle state | Standard library |
| `syncwheel_index` | Git index decoding and semantic content leases | Standard library |
| `syncwheel_stacks` | Stack creation, close/archive, draft promotion, rebuild and push commands | Selected manifest inputs; composed coordination/replay/worktree services; explicit Git/control/ledger/projection ports |
| `syncwheel_revision_backend` | Delivery/journal revision transactions, recovery and index leases | Explicit repository, manifest, ledger, guard and provenance capabilities; domain service factories |
| `syncwheel_files` | Durable byte writes and directory fsync | Standard library |
| `syncwheel_provenance` | Derived identity, durable provenance store and override/reset policy | Explicit Git observations and coordination-state evidence |
| `syncwheel_replay` | Mode selection, replay planning/execution, product projections | Separate Git, resolution and checkout-lease ports |
| `syncwheel_worktrees` | Registry CAS, lifecycle recovery, capture and retirement | Git, lifecycle ledger, durable storage and phase observer ports |
| `syncwheel_cli` | Argument grammar and command behavior descriptions | Schema vocabulary; named handler registry |
| `syncwheel_contracts` | Shared CLI/publication choice values | Manifest vocabulary |
| `syncwheel_errors` | Exception identities shared across domains | None |

Domains never import the CLI facade. File loading and command dispatch remain application responsibilities. Journal
transaction sequencing and locking belong to `JournalWorkflow`; its explicit
`JournalRepository` protocol supplies only the I/O operations those transactions
use. The facade adapter delegates each operation to the current application
function so existing `git`, runner, file-reader and fingerprint patches still apply. The facade retains application entry points used by other components. Domain
helpers are internal: tests inject races and failures at the owning module or an
explicit port, preserving their original phases and assertions. Functions that cross
an I/O boundary use small adapters: journal admission receives the current
facade reader; coordination receives the current branch validator. The parser
receives a registry built from the current command handlers on every invocation,
so replacing a facade handler before building a parser remains supported.

The package manifest lists every module explicitly. Adding a source module also
requires updating `pyproject.toml`; source-only success does not prove wheel
contents. `test_module_boundaries.py` checks isolated direct-file loading, shared
exceptions, handler replacement, journal reader replacement and Git validation.
Existing integration suites continue to exercise journal, channel, coordination,
ledger and index behavior through the supported facade.

This is incremental modularization. Integration reconciliation and remaining integration/channel command handlers still live in the
composition root. Public backend constructors compose the implementation modules
without changing the revision-provider wire protocol. Future extraction should introduce specific repository operations at
those boundaries, preserve existing patch points, and run the corresponding
integration suites. Do not use shared-global execution, wildcard imports, module
proxies or a generic object that forwards arbitrary facade globals.

Vendored installations must copy the complete `scripts/syncwheel*.py` sibling set
and `VERSION` together; a single copied CLI file is no longer self-contained.
The invocation remains `python3 path/to/bundle/syncwheel.py`, and updates remain
manual. The isolated vendored smoke runs with `-I -S` and no `PYTHONPATH`.
