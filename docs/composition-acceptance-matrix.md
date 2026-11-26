# Composition acceptance matrix

This is a coverage map, not a declaration that every scenario is qualified.
Each run must record the source revision, command, outcome and limitations.
Source tests, installed CLI checks and execution-slot qualification are separate.

The synthetic input has ten source stacks: A/B/C are drafts; C has no available
author; D/E are wanted and valid; F is a valid hotfix incompatible with E; G is
J's prerequisite; H is valid but not selected; I is ready but fails its check;
J depends on G. Author availability is not an independence criterion.

## Existing capabilities and remaining checks

`channels` below means `tests/test_channels.py`; `coordination` means
`tests/test_coordination.py`. A named existing test is a candidate evidence
source, not a substitute for a recorded run on the accepted revision.

| Case | Required behavior | Evidence seam / remaining qualification |
|---|---|---|
| 1 | Draft → ready does not select a staging composition | `channels.test_state_only_stack_promote_keeps_pinned_channel_valid`; verify selected composition/ref unchanged. |
| 2 | Nine problematic foreign stacks do not block an independent target | `test_selective_history_scope.py`, `test_selective_ref_scope.py`, coordination missing-foreign CLI cases; scope is compose v2. |
| 3 | Lost author immediately or after twenty minutes creates no artificial wait | `test_execution_policy.py` authorized-writer case; explicit time/availability matrix remains. |
| 4 | No transcript is needed; local work survives | `test_published_recipe_fresh_clone.py`; add explicit absent-transcript assertion. |
| 5 | Returning author sees preserved work | Foreign-lane preservation is covered; returning-author continuation remains. |
| 6 | Valid unwanted H is not selected | `test_composition_acceptance.py` unwanted/broken-source case. |
| 7 | Broken ready I does not block independent work or become implicitly green | Same test: independent composition omits I; selecting I still fails its actual syntax check. |
| 8 | Incompatible E/F can be qualified separately | `test_composition_acceptance.py` conflicting-choices case; separate channel tips contain their exact alternative. |
| 9 | Explicit required E+F fails on their conflict | Same case; native replay refuses the required union. |
| 10 | Removing E does not reuse its old green proof | Same case; recipe digest changes, old tip remains until a separate apply. Behavioral-test receipts remain separate. |
| 11 | Last valid composition survives a failed attempt | Same case; failed union preserves both previously valid channel refs. |
| 12 | Broken G blocks J, not independent work | `channels.test_dependency_closure_and_order_are_fail_closed`; add the explicit G/J versus D split. |
| 13 | Delivered G permits J on a new base after reevaluation | Explicit absorbed-prerequisite/new-base scenario remains. |
| 14 | Abandoning G cannot cosmetically erase J's dependency | `channels.test_stack_close_refuses_dependents_without_side_effects`; verify explicit G/J instance. |
| 15 | New SHA invalidates pertinent evidence | `channels.test_pins_are_immutable_until_explicit_refresh` plus exact-head execution evidence. |
| 16 | New base reevaluates pertinent evidence | `channels.test_base_is_pinned_until_refresh_deliberately_repins_it`; behavioral evidence on refreshed base remains. |
| 17 | A hidden dependency is detected | Explicit `depends_on` and base-owner closure exist; undeclared semantic dependency must be exercised by an actual check. No universal inference is claimed. |
| 18 | Independent CAS operations serialize safely | Fresh-clone contention/retry fixture plus channel manifest-lock/CAS tests; no simultaneous-success promise for one shared state ref. |
| 19 | Real ref/claim collision still refuses | Channel create ownership/collision tests and coordination required-scope/claim guards. |
| 20 | Crash before push / after remote success before receipt recovers safely | Coordination promote pre-push and selective-compose post-push SIGKILL cases; channel receipt reconciliation tests. |
| 21 | Tests/review hold no publication lock | Runtime slot/lock measurement remains; short atomic mutation locks are distinct. |
| 22 | Unavailable CI localizes the wait to the affected target | Exact-head live CI evidence exists; selective two-target unavailable-CI scenario remains. |
| 23 | Physical integrity is invariant across all four presets | Disabled/minimal installed checks exist; standard/strict and four-profile matrix remain separate. |
| 24 | Inheritance, override, clear and provenance are explicit | Execution/worktree/lane policy suites; full four-profile cross-scope matrix remains. |

The first composition fixture reuses the existing channel harness and manifest
migration. It does not rebuild shared integration, install services, contact
authors, modify application preferences or assert deployment. Channel apply
proves materialization and records a composition digest; it does not manufacture
a successful test, review, CI result or deployment receipt.
