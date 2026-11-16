"""Explicit, recoverable adoption of an existing stack history without replay."""

import copy
import json

from syncwheel_errors import SyncwheelError
from syncwheel_manifest import canonical_json_digest, manifest_digest, require_stack


def adoption_checkpoint(stage):
    """Fault-injection seam for the two durable writes."""


def pending_adoptions(events):
    completed = {
        event['payload']['planDigest'] for event in events
        if event.get('type') == 'stack_tip_adopted'
    }
    return [event['payload'] for event in events
            if event.get('type') == 'stack_tip_adoption_prepared'
            and event['payload']['planDigest'] not in completed]


class StackAdoption:
    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.repository = lifecycle.repository
        self.root = self.repository.root

    def plan(self, manifest, stack, base_ref, tip_ref):
        repo = self.repository
        base = repo.commit_full_sha(self.root, base_ref)
        tip = repo.commit_full_sha(self.root, tip_ref)
        if not repo.branch_contains(self.root, tip, base):
            raise SyncwheelError('stack adoption requires the base to be an ancestor of the exact tip')
        proposed = copy.deepcopy(manifest)
        updated = require_stack(proposed, stack['id'])
        updated['base'] = base
        updated['commits'] = [] if tip == base else [tip]
        updated['adopted_history'] = {'base': base, 'tip': tip}
        # Integration resolutions bind the previous source recipe, not this one.
        for field in ('integration_commits', 'integration_resolution'):
            updated.pop(field, None)
        # The supported recipe is the exact tip against its direct base
        # parent. Its delta therefore produces the existing tip tree without
        # a projection checkout, temporary index, or new Git objects.
        if tip != base:
            parents = repo.git(self.root, 'rev-list', '--parents', '-n', '1', tip).stdout.split()[1:]
            contained = [parent for parent in parents if repo.branch_contains(self.root, base, parent)]
            if contained != [base]:
                raise SyncwheelError('stack adoption requires the base to be the unique direct projection parent')
        if repo.git(self.root, 'symbolic-ref', '-q', f"refs/heads/{stack['branch']}", check=False).returncode == 0:
            raise SyncwheelError('stack adoption requires a direct source ref, not a symbolic ref')
        old_tip = repo.ref_tip(self.root, stack['branch'])
        if old_tip and not repo.branch_contains(self.root, tip, old_tip):
            raise SyncwheelError('stack adoption would discard the current local source history')
        if old_tip != tip and repo.find_worktree_for_branch(self.root, stack['branch']):
            raise SyncwheelError('stack adoption cannot move a branch checked out in a worktree')
        publication_remote = stack.get('publication_remote') or manifest['defaults']['publication_remote']
        observed_remote = repo.ref_tip(self.root, f"{publication_remote}/{stack['branch']}")
        if observed_remote and not repo.branch_contains(self.root, tip, observed_remote):
            raise SyncwheelError('stack adoption would discard the observed remote source history')
        history = repo.rev_list(self.root, f'{base}..{tip}')
        adopted = set(history)
        for other in manifest['stacks']:
            if other['id'] == stack['id']:
                continue
            other_history = other.get('adopted_history')
            owned = (repo.rev_list(self.root, f"{other_history['base']}..{other_history['tip']}")
                     if other_history else [repo.commit_full_sha(self.root, c) for c in other['commits']])
            if adopted.intersection(owned):
                raise SyncwheelError(f"stack adoption overlaps declared history of stack {other['id']!r}")
        plan = {
            'stack': stack['id'], 'branch': stack['branch'],
            'oldTip': old_tip, 'base': base, 'tip': tip,
            'observedRemoteTip': observed_remote, 'history': history,
            'beforeDigest': manifest_digest(manifest), 'afterDigest': manifest_digest(proposed),
            'stackAfter': updated,
        }
        plan['planDigest'] = canonical_json_digest(plan)
        return plan

    def execute_stack_adoption(self, args, manifest, manifest_path):
        if not args.base or not args.adopt_tip or args.specs or args.published or args.target_branch:
            raise SyncwheelError('stack set adoption requires --base and --adopt-tip, without other edits')
        repo = self.repository
        events = self.lifecycle.ledger.load_ledger_events(self.root, manifest_path)
        pending = pending_adoptions(events)
        plan = next((p for p in pending if p['planDigest'] == args.plan_digest), None)
        if plan is None:
            if pending:
                raise SyncwheelError('an interrupted stack adoption must be resumed with --plan-digest '
                                     + pending[0]['planDigest'] + ' --apply')
            plan = self.plan(manifest, require_stack(manifest, args.stack), args.base, args.adopt_tip)
        if (plan['stack'] != args.stack or plan['base'] != repo.commit_full_sha(self.root, args.base)
                or plan['tip'] != repo.commit_full_sha(self.root, args.adopt_tip)):
            raise SyncwheelError('adoption arguments do not match the prepared operation')
        if canonical_json_digest({k: v for k, v in plan.items() if k != 'planDigest'}) != plan['planDigest']:
            raise SyncwheelError('prepared stack adoption digest is invalid')
        print(json.dumps(plan, sort_keys=True))
        if not args.apply:
            return 0
        if args.plan_digest != plan['planDigest']:
            raise SyncwheelError('stack adoption requires the exact preview --plan-digest')
        self.lifecycle.control.require_manifest_transaction_current(manifest_path)
        if repo.git(self.root, 'symbolic-ref', '-q', f"refs/heads/{plan['branch']}", check=False).returncode == 0:
            raise SyncwheelError('stack adoption requires a direct source ref, not a symbolic ref')
        current_digest = manifest_digest(manifest)
        current_tip = repo.ref_tip(self.root, plan['branch'])
        expected_states = {
            (plan['beforeDigest'], plan['oldTip']),
            (plan['beforeDigest'], plan['tip']),
            (plan['afterDigest'], plan['tip']),
        }
        if (current_digest, current_tip) not in expected_states:
            raise SyncwheelError('stack adoption state changed; preserving the observed manifest and ref')
        if current_tip != plan['tip'] and repo.find_worktree_for_branch(self.root, plan['branch']):
            raise SyncwheelError('stack adoption cannot move a branch checked out in a worktree')
        if plan not in pending:
            self.lifecycle.ledger.append_ledger_event(
                self.root, 'stack_tip_adoption_prepared', plan, manifest_path,
            )
        adoption_checkpoint('prepared')
        if repo.git(self.root, 'symbolic-ref', '-q', f"refs/heads/{plan['branch']}", check=False).returncode == 0:
            raise SyncwheelError('stack adoption requires a direct source ref, not a symbolic ref')
        if current_tip != plan['tip'] and repo.find_worktree_for_branch(self.root, plan['branch']):
            raise SyncwheelError('stack adoption cannot move a branch checked out in a worktree')
        if current_tip != plan['tip']:
            changed = repo.git(self.root, 'update-ref', '--no-deref', f"refs/heads/{plan['branch']}",
                               plan['tip'], plan['oldTip'] or ('0' * len(plan['tip'])), check=False)
            if changed.returncode:
                raise SyncwheelError('stack source changed during adoption; prepared operation retained')
        adoption_checkpoint('ref-updated')
        self.lifecycle.control.require_manifest_transaction_current(manifest_path)
        if repo.ref_tip(self.root, plan['branch']) != plan['tip']:
            raise SyncwheelError('stack source changed after adoption; prepared operation retained')
        if current_digest != plan['afterDigest']:
            proposed = copy.deepcopy(manifest)
            proposed['stacks'] = [copy.deepcopy(plan['stackAfter']) if s['id'] == args.stack else s
                                  for s in proposed['stacks']]
            if manifest_digest(proposed) != plan['afterDigest']:
                raise SyncwheelError('prepared stack adoption does not match the manifest preimage')
            self.lifecycle.control.save_manifest_with_ledger(
                self.root, manifest_path, proposed, 'stack_tip_adoption',
                {'stack': args.stack, 'planDigest': plan['planDigest']},
            )
        adoption_checkpoint('manifest-updated')
        if repo.ref_tip(self.root, plan['branch']) != plan['tip']:
            raise SyncwheelError('stack source changed after manifest adoption; prepared operation retained')
        self.lifecycle.ledger.append_ledger_event(
            self.root, 'stack_tip_adopted', {'stack': args.stack, 'planDigest': plan['planDigest']}, manifest_path,
        )
        return 0
