# Source module boundaries

`syncwheel.py` remains the supported CLI and import facade. Direct execution,
installed entry points, and loading it with `importlib.util.spec_from_file_location`
resolve the same sibling modules. Runtime version and source checkout detection
remain in that facade, where `__file__` still identifies the entry script.

| Module | Responsibility | Dependencies |
| --- | --- | --- |
| `syncwheel_manifest` | Manifest normalization, authority, stack dependencies, channel schema | Shared errors; standard library |
| `syncwheel_coordination` | Snapshot validation, portable refs, proposal comparison and composition | Manifest domain; explicit branch validator |
| `syncwheel_journal` | File admission, status decoding, safe content observation | Shared errors; explicit status and reader inputs |
| `syncwheel_journal_workflow` | Snapshot, publish, pull and recovery sequencing | Journal policy; explicit repository I/O port |
| `syncwheel_ledger` | Event reduction into lifecycle state | Standard library |
| `syncwheel_index` | Git index decoding and semantic content leases | Standard library |
| `syncwheel_cli` | Argument grammar and command behavior descriptions | Schema vocabulary; named handler registry |
| `syncwheel_contracts` | Shared CLI/publication choice values | Manifest vocabulary |
| `syncwheel_errors` | Exception identities shared across domains | None |

Domains never import the CLI facade. File loading and command dispatch remain application responsibilities. Journal
transaction sequencing and locking belong to `JournalWorkflow`; its explicit
`JournalRepository` protocol supplies only the I/O operations those transactions
use. The facade adapter delegates each operation to the current application
function so existing `git`, runner, file-reader and fingerprint patches still apply. The facade re-exports
existing function and constant names for import consumers. Functions that cross
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

This is incremental modularization. Mutable coordination publication, replay,
managed worktree lifecycle and revision-provider orchestration still live in the
facade. Future extraction should introduce specific repository operations at
those boundaries, preserve existing patch points, and run the corresponding
integration suites. Do not use shared-global execution, wildcard imports, module
proxies or a generic object that forwards arbitrary facade globals.
