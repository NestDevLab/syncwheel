# Native maintenance delivery

An explicit maintenance mandate may select native Git and the forge instead of
Syncwheel for maintaining Syncwheel and its installation resources. Use an
independent checkout; do not invoke the installed CLI, its revision provider or
its hooks transitively in that checkout. This exception does not change an
application repository's manifest, authority, coordination or server rules.

Before writing, identify the exact repository, base, current owner and unpublished
work. Keep one writer per diff and preserve existing lanes and shared checkouts.
Deliver small commits with focused tests and independent review for configuration
or integrity changes. Verify the exact candidate and required forge checks, then
merge with the expected head pinned and without an administrator bypass.

Install the exact merged revision using the existing package manager or artifact
installer. For agent resources, use Agentwheel from the workspace that already
owns the target runtime paths. Its effective revision provider must be native Git
for this maintenance route; retain reason, journal, CAS and commit verification.
A different checkout or profile is not permission to take over runtime ownership.
Qualify the installer in an isolated fixture, including its subprocess trace,
then inspect an intended-only plan before applying to the authorized owner.

Verify installed version, source pin and module/resource bytes. Run the smoke in
an isolated user/repository fixture and compare protected settings, manifests,
registries, refs and unrelated checkouts with the preflight. Retain the rollback
pin and receipts. Report source, CI, merge, install and smoke as separate states;
installation does not activate preferences in application repositories.

## Operation boundary

Syncwheel remains the route for its managed stacks, lanes, composition and
publication. Ordinary Git reads, edits, commits and tests in the assigned source surface do
not require a global integration rebuild. Shared-primary, managed-ref and
publication contracts still apply. Honor the selected execution policy
only where the installed consumer implements it. Unset configuration retains
legacy behavior. A preset never creates test/review evidence or waives forge
rules, real ownership, Git integrity, CAS or published multiwriter contracts.
Do not contact an author or wait for inactivity to classify unrelated work:
use the selected target, dependency closure and verified remote bindings.
