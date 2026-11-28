"""Verified attribution and shared recovery CAS, independent of lane identity."""

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from syncwheel_actor_recovery import (
    GitRecoveryStore, PATH, delivery_blockers, empty_state, run_command, transition,
)
import syncwheel_actor_recovery as recovery_module
STATE_REF = getattr(recovery_module, 'STATE_REF', 'refs/heads/syncwheel/actor-recovery')
from syncwheel_errors import SyncwheelError

REPO = 'fixture-repository'
HEAD = 'a' * 40


def authority():
    return {'schema_version': 1, 'repository': REPO, 'records': {}}


def record(source, key, kind, **fields):
    source['records'][key] = dict(kind=kind, repository=REPO, **fields)
    return key


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.state, self.authority = empty_state(REPO), authority()

    def apply(self, action, reference, **kwargs):
        self.state = transition(self.state, self.authority, action, reference, **kwargs)

    def incident(self, number, actor='agent-a', delivery='delivery-a', recover=True):
        key = record(self.authority, 'proof-' + number, 'incident', incident=number,
                     actor=actor, delivery=delivery, proven=True, attributed=True,
                     evidence='independent-investigation-' + number)
        self.apply('confirm', key)
        if recover:
            key = record(self.authority, 'recovery-' + number, 'recovery', incident=number,
                         recovered=True, attribution_verified=True, evidence='verified-recovery-' + number)
            self.apply('recover', key)

    def publication(self, operation='publish-1', actor='agent-a', delivery='another-lane', head=HEAD):
        return record(self.authority, operation, 'publication', operation=operation,
                      actor=actor, delivery=delivery, head=head)

    def review(self, operation='publish-1', actor='agent-a', head=HEAD, reviewer='agent-reviewer'):
        return record(self.authority, 'review-' + operation, 'review', operation=operation,
                      actor=actor, head=head, reviewer=reviewer, outcome='approved', evidence='review-record')

    def outcome(self, outcome='published', operation='publish-1', actor='agent-a', delivery='another-lane'):
        return record(self.authority, 'outcome-' + operation, 'outcome', operation=operation,
                      actor=actor, delivery=delivery, head=HEAD, outcome=outcome, evidence='remote-observation')

    def test_warning_reporter_and_unverified_claim_never_penalize(self):
        original = copy.deepcopy(self.state)
        for proven, attributed in ((False, True), (True, False), (False, False)):
            key = record(self.authority, 'unverified', 'incident', incident='one',
                         actor='reporter', delivery='delivery-a', proven=proven,
                         attributed=attributed, evidence='warning')
            with self.assertRaises(SyncwheelError):
                self.apply('confirm', key)
            self.assertEqual(self.state, original)
        with self.assertRaises(SyncwheelError):
            self.apply('confirm', 'forged-local-receipt')

    def test_scope_recovery_and_idempotence(self):
        self.incident('one', recover=False)
        before = copy.deepcopy(self.state)
        self.apply('confirm', 'proof-one')
        self.assertEqual(self.state, before)
        self.assertEqual(delivery_blockers(self.state, 'delivery-a'), ['one'])
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', self.publication(delivery='delivery-a'))
        self.apply('prepare', self.publication(actor='unrelated-agent'))
        # The lane successor is blocked only while this delivery is unrecovered.
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', self.publication('successor', 'successor', 'delivery-a'))
        key = record(self.authority, 'recovered', 'recovery', incident='one', recovered=True,
                     attribution_verified=True, evidence='preserved-work-and-author')
        self.apply('recover', key)
        self.apply('recover', key)
        self.apply('prepare', 'successor')

    def test_second_distinct_incident_requires_exact_independent_review_across_lanes(self):
        self.incident('one')
        self.apply('confirm', 'proof-one')
        self.apply('prepare', self.publication('first'))  # duplicate is not a second incident
        self.incident('two')
        self.apply('prepare', self.publication('other', actor='successor'))
        # First uncertain publication must be reconciled before another attempt.
        self.apply('complete', self.outcome(operation='first'))
        publication = self.publication()
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', publication)
        for head, reviewer in (('b' * 40, 'reviewer'), (HEAD, 'agent-a')):
            with self.assertRaises(SyncwheelError):
                self.apply('prepare', publication, review=self.review(head=head, reviewer=reviewer))
        self.apply('prepare', publication, review=self.review())
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', publication, review='review-publish-1')
        self.assertNotIn('agent-a', self.state['reviewed_through'])
        self.apply('complete', self.outcome())
        before = copy.deepcopy(self.state)
        self.apply('complete', 'outcome-publish-1')
        self.assertEqual(self.state, before)
        self.apply('prepare', self.publication('next'))

    def test_failed_or_uncertain_publication_does_not_consume_review(self):
        self.incident('one')
        self.incident('two')
        self.apply('prepare', self.publication(), review=self.review())
        with self.assertRaises(SyncwheelError):
            self.apply('complete', self.outcome('unknown'))
        self.apply('complete', self.outcome('not_published'))
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', self.publication('retry'))
        self.apply('prepare', 'retry', review=self.review('retry'))
        self.incident('three')
        self.apply('complete', self.outcome(operation='retry'))
        self.assertEqual(self.state['reviewed_through']['agent-a'], ['one', 'two'])
        with self.assertRaises(SyncwheelError):
            self.apply('prepare', self.publication('after-new-incident'))

    def test_incident_or_terminal_collision_cannot_rewrite_evidence(self):
        self.incident('one')
        self.authority['records']['proof-one']['actor'] = 'successor'
        with self.assertRaises(SyncwheelError):
            self.apply('confirm', 'proof-one')
        self.apply('prepare', self.publication())
        self.apply('complete', self.outcome())
        with self.assertRaises(SyncwheelError):
            self.apply('complete', self.outcome('not_published'))


class SharedGitTest(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='actor-recovery-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / 'remote.git'
        self.remote.mkdir()
        self.git(self.remote, 'init', '--bare')
        self.clones = []
        for name in ('writer', 'peer'):
            clone = self.root / name
            self.git(self.root, 'clone', str(self.remote), str(clone))
            self.git(clone, 'config', 'user.name', 'Fixture')
            self.git(clone, 'config', 'user.email', 'fixture@example.invalid')
            self.clones.append(clone)
        self.state_ref, self.authority_ref = STATE_REF, 'refs/heads/recovery-authority'
        self.provision(self.state_ref, empty_state(REPO))
        source = authority()
        for number in ('one', 'two'):
            record(source, number, 'incident', incident=number, actor='agent-a', delivery=number,
                   proven=True, attributed=True, evidence='maintainer-investigation-' + number)
        self.provision(self.authority_ref, source)
        self.stores = [GitRecoveryStore(clone, 'origin', self.state_ref, self.authority_ref, REPO)
                       for clone in self.clones]

    def provision(self, ref, document):
        clone = self.clones[0]
        path = clone / PATH
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(document))
        self.git(clone, 'add', PATH)
        self.git(clone, 'commit', '-m', 'Fixture authority')
        self.git(clone, 'push', 'origin', 'HEAD:' + ref)

    def test_two_clones_share_state_stale_cas_refuses_and_preserves_worktrees(self):
        first, second = self.stores
        stale = second.snapshot()
        before = [self.git(clone, 'status', '--porcelain') for clone in self.clones]
        sha = first.apply(first.snapshot(), 'confirm', 'one')
        with self.assertRaises(SyncwheelError):
            second.apply(stale, 'confirm', 'two')
        self.assertEqual(second.snapshot()['state_sha'], sha)
        result = run_command(second, 'confirm', 'two')
        self.assertFalse(result['applied'])
        run_command(second, 'confirm', 'two', apply=True)
        self.assertEqual(sorted(first.snapshot()['state']['incidents']), ['one', 'two'])
        current = first.snapshot()
        self.assertEqual(first.apply(current, 'confirm', 'one'), current['state_sha'])
        self.assertEqual(before, [self.git(clone, 'status', '--porcelain') for clone in self.clones])

    def test_missing_store_or_wrong_repository_fails_closed(self):
        store = GitRecoveryStore(self.clones[0], 'origin', 'refs/heads/missing', self.authority_ref, REPO)
        with self.assertRaises(SyncwheelError):
            store.snapshot()
        store = GitRecoveryStore(self.clones[0], 'origin', self.state_ref, self.authority_ref, 'wrong')
        with self.assertRaises(SyncwheelError):
            store.snapshot()

    def test_actual_remote_cas_race_never_loses_peer_event(self):
        writer, peer = self.stores
        before = writer.snapshot()
        native_git = writer.git

        def racing_git(*args, **kwargs):
            if args[0] == 'push':
                peer.apply(peer.snapshot(), 'confirm', 'two')
            return native_git(*args, **kwargs)

        with mock.patch.object(writer, 'git', side_effect=racing_git):
            with self.assertRaisesRegex(SyncwheelError, 'Git operation refused'):
                writer.apply(before, 'confirm', 'one')
        self.assertEqual(list(writer.snapshot()['state']['incidents']), ['two'])

    def test_independent_authority_permission_and_local_forgery(self):
        before = self.stores[0].snapshot()['authority_sha']
        hook = self.remote / 'hooks/pre-receive'
        hook.write_text('#!/bin/sh\nwhile read old new ref; do\n'
                        '  if [ "$ref" = "refs/heads/recovery-authority" ]; then\n'
                        '    echo "authority updates require independent maintainer" >&2\n'
                        '    exit 1\n  fi\ndone\n')
        hook.chmod(0o755)
        forged = authority()
        record(forged, 'forged', 'incident', incident='forged', actor='successor',
               delivery='unrelated', proven=True, attributed=True, evidence='self-claim')
        with self.assertRaises(subprocess.CalledProcessError):
            self.provision(self.authority_ref, forged)
        with self.assertRaisesRegex(SyncwheelError, 'independent authority attestation'):
            run_command(self.stores[1], 'confirm', 'forged', apply=True)
        self.assertEqual(self.stores[1].snapshot()['authority_sha'], before)
        self.assertEqual(self.stores[1].snapshot()['state']['incidents'], {})


class MergeEntryPointTest(unittest.TestCase):
    """Existing real merge handler; fake only the external forge observation."""

    def setUp(self):
        import test_github_pr_merge as fixtures
        self.app = fixtures.SYNCWHEEL
        self.fixture = fixtures.GithubPrMergeTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.repo
        self.repository = self.fixture.policy()['repository']
        self.state_ref, self.authority_ref = STATE_REF, 'refs/heads/recovery-authority'
        self.store = GitRecoveryStore(self.root, 'origin', self.state_ref, self.authority_ref, self.repository)
        self.profile = {'hooks': {'mode': 'required'}, 'github_pr_merge': self.fixture.policy(),
                        'actor_recovery': {'enabled': True, 'remote': 'origin',
                            'state_ref': self.state_ref, 'authority_ref': self.authority_ref,
                            'repository': self.repository, 'product_refs': ['refs/heads/main']}}
        self.write_profile()
        self.fixture.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.fixture.manifest_path.write_text(json.dumps(self.fixture.manifest) + '\n')
        self.authority = {'schema_version': 1, 'repository': self.repository, 'records': {}}
        initial = empty_state(self.repository)
        initial['policy'] = self.profile['actor_recovery']
        self.provision(self.state_ref, initial)
        self.provision(self.authority_ref, self.authority)

    def write_profile(self):
        (self.root / '.syncwheel/profile.local.json').write_text(json.dumps(self.profile))

    def peer_store(self):
        peer = self.fixture.tmp / 'peer'
        subprocess.run(['git', 'clone', '-q', '--no-checkout', str(self.fixture.remote), str(peer)], check=True)
        subprocess.run(['git', '-C', str(peer), 'config', 'user.name', 'Peer'], check=True)
        subprocess.run(['git', '-C', str(peer), 'config', 'user.email', 'peer@example.invalid'], check=True)
        return GitRecoveryStore(peer, 'origin', self.state_ref, self.authority_ref, self.repository)

    def provision(self, ref, document):
        blob = self.store.git('hash-object', '-w', '--stdin', input=json.dumps(document))
        subtree = self.store.git('mktree', input='100644 blob ' + blob + '\tactor-recovery.json\n')
        tree = self.store.git('mktree', input='040000 tree ' + subtree + '\t.syncwheel\n')
        prior = self.store.git('ls-remote', '--refs', 'origin', ref)
        parent = ['-p', prior.split()[0]] if prior else []
        commit = self.store.git('commit-tree', tree, *parent, input='Fixture attestation\n')
        self.store.git('push', 'origin', commit + ':' + ref)

    def attest(self, key, kind, **fields):
        self.authority['records'][key] = dict(kind=kind, repository=self.repository, **fields)
        self.provision(self.authority_ref, self.authority)
        return key

    def preview(self, reference='publication', review=None):
        args = self.fixture.args()
        args.actor_publication, args.actor_review = reference, review
        with mock.patch.object(self.app, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(self.app, 'github_adapter_request', return_value=self.fixture.observation()):
            return self.app.build_github_pr_merge_plan(self.root, self.fixture.manifest,
                                                      self.fixture.manifest_path, 'feature', args)

    def binding(self, actor='responsible-agent', delivery='product'):
        return self.attest('publication', 'publication', operation='merge-1', actor=actor,
                           delivery=delivery, target_ref='refs/heads/main', head=self.fixture.head, base_sha=self.fixture.base,
                           base_branch='main', pull_request=25)

    def incidents(self, actor='responsible-agent'):
        for incident in ('one', 'two'):
            proof = self.attest(incident, 'incident', incident=incident, actor=actor,
                                delivery='prior-delivery', proven=True, attributed=True,
                                evidence='independent-proof-' + incident)
            self.store.apply(self.store.snapshot(), 'confirm', proof)
            recovered = self.attest('recover-' + incident, 'recovery', incident=incident,
                                    recovered=True, attribution_verified=True, evidence='recovery-' + incident)
            self.store.apply(self.store.snapshot(), 'recover', recovered)

    def apply_merge(self, plan, binding, review=None, *, interruption=False):
        args = self.fixture.args(apply=True, plan_digest=plan['planDigest'])
        args.actor_publication, args.actor_review = binding, review
        merged = self.fixture.observation(state='MERGED', decision='APPROVED')
        merged['pr'].update(mergeCommit='f' * 40, mergedAt='2026-01-01T00:00:00Z')
        results = [self.fixture.observation(), {'ok': True}, merged]
        if interruption:
            results[2] = RuntimeError('lost remote response')
        with mock.patch.object(self.app, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(self.app, 'github_adapter_request', side_effect=results), \
             mock.patch.object(self.app, 'github_pr_merge_postverify', return_value={'status': 'verified'}), \
             contextlib.redirect_stdout(io.StringIO()):
            return self.app.command_stack_merge_pr(args)

    def test_normal_new_lane_author_and_unrelated_agent_are_not_penalized(self):
        self.incidents(actor='another-agent')
        binding = self.binding(actor='legitimate-successor')
        self.assertEqual(self.apply_merge(self.preview(), binding), 0)
        state = self.store.snapshot()['state']
        self.assertEqual(state['operations']['merge-1']['outcome']['outcome'], 'published')
        self.assertEqual(state['reviewed_through'], {})

    def test_different_delivery_to_same_product_target_is_not_globally_held(self):
        proof = self.attest('affected-work', 'incident', incident='affected-work',
                            actor='responsible-agent', delivery='affected-work',
                            proven=True, attributed=True, evidence='independent-proof')
        self.store.apply(self.store.snapshot(), 'confirm', proof)
        binding = self.binding(actor='legitimate-successor', delivery='unrelated-work')
        self.assertEqual(self.apply_merge(self.preview(), binding), 0)

    def test_exact_review_required_then_consumed_only_on_remote_success(self):
        self.incidents()
        binding = self.binding()
        plan = self.preview()
        with self.assertRaises(SyncwheelError):
            self.apply_merge(plan, binding)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})
        review = self.attest('review', 'review', operation='merge-1', actor='responsible-agent',
                             reviewer='independent-reviewer', head=self.fixture.head,
                             outcome='approved', evidence='exact-review')
        self.assertEqual(self.apply_merge(self.preview(review=review), binding, review), 0)
        self.assertEqual(self.store.snapshot()['state']['reviewed_through']['responsible-agent'], ['one', 'two'])

    def test_crash_after_real_publication_reservation_native_readback_is_idempotent(self):
        from syncwheel_actor_recovery import reconcile_reference
        binding = self.binding()
        with self.assertRaisesRegex(RuntimeError, 'lost remote response'):
            self.apply_merge(self.preview(), binding, interruption=True)
        self.assertIsNone(self.store.snapshot()['state']['operations']['merge-1']['outcome'])
        unknown = self.fixture.observation(state='CLOSED')
        self.assertEqual(reconcile_reference(self.root, self.profile, binding, lambda request: unknown)['status'], 'unknown')
        merged = self.fixture.observation(state='MERGED')
        merged['pr'].update(mergeCommit='f' * 40, mergedAt='2026-01-01T00:00:00Z')
        for _ in range(2):
            result = reconcile_reference(self.root, self.profile, binding, lambda request: merged)
            self.assertEqual(result['status'], 'published')
        self.assertEqual(len(self.store.snapshot()['state']['operations']), 1)

    def test_alternative_real_product_push_refused_work_ref_and_nonoptin_unchanged(self):
        for command in (
                ['git', 'push', 'origin', self.fixture.head + ':refs/heads/main'],
                ['git', '-c', 'advice.pushUpdateRejected=false', 'push', '--repo=origin', self.fixture.head + ':main'],
                ['git', 'push', '--repo', 'origin', self.fixture.head + ':main']):
            with self.assertRaisesRegex(SyncwheelError, 'product target'):
                self.app.run(command, cwd=self.root)
        self.app.run(['git', 'push', 'origin', self.fixture.head + ':refs/heads/another-work-lane'], cwd=self.root)
        del self.profile['actor_recovery']
        self.write_profile()
        # A dry product push follows ordinary Git/server rules when not opted in.
        result = self.app.run(['git', 'push', '--dry-run', 'origin', self.fixture.head + ':refs/heads/main'], cwd=self.root)
        self.assertEqual(result.returncode, 0)

    def test_review_r1_deleted_local_optin_cannot_clear_remote_actor_requirement(self):
        self.incidents()
        self.binding()
        del self.profile['actor_recovery']
        self.write_profile()
        plan = self.preview()
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(self.store.snapshot()['state']['operations'], {})
        peer = self.peer_store()
        self.assertFalse((peer.root / '.syncwheel/profile.local.json').exists())
        report = recovery_module.preview_merge(peer.root, {}, plan, reference='publication')
        self.assertEqual(report['status'], 'blocked')

    def test_review_r2_local_target_change_cannot_remove_canonical_gate(self):
        binding = self.binding()
        plan = self.preview()
        self.profile['actor_recovery']['product_refs'] = []
        self.write_profile()
        reserved = recovery_module.reserve_merge(self.root, self.profile, plan, reference=binding)
        self.assertIsNotNone(reserved)

    def test_review_r2_canonical_target_policy_changed_after_preview_refuses_before_reservation(self):
        binding = self.binding()
        plan = self.preview()
        state = self.store.snapshot()['state']
        state['policy']['product_refs'] = ['refs/heads/other-product']
        self.provision(self.state_ref, state)
        with self.assertRaisesRegex(SyncwheelError, 'policy changed'):
            recovery_module.reserve_merge(self.root, self.profile, plan, reference=binding)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})

    def test_review_r2_policy_changes_after_reservation_refuse_effect(self):
        binding = self.binding()
        plan = self.preview()
        recovery_module.reserve_merge(self.root, self.profile, plan, reference=binding)
        state = self.store.snapshot()['state']
        state['policy']['product_refs'] = ['refs/heads/other-product']
        self.provision(self.state_ref, state)
        with self.assertRaisesRegex(SyncwheelError, 'policy changed'):
            recovery_module.start_merge(self.root, plan, binding)
        self.assertEqual(self.store.snapshot()['state']['operations']['merge-1']['phase'], 'prepared')

    def test_review_r1_manifest_remote_alias_cannot_redirect_forge_policy(self):
        self.incidents()
        binding = self.binding()
        plan = self.preview()
        plan['targetRemote'] = 'candidate-chosen-fork'
        report = recovery_module.preview_merge(self.root, {}, plan, reference=binding)
        self.assertEqual(report['status'], 'blocked')

    def test_review_r3_real_remote_cas_confirm_beats_effect_start(self):
        binding = self.binding()
        proof = self.attest('late-proof', 'incident', incident='late-proof', actor='responsible-agent',
                            delivery='product', proven=True, attributed=True, evidence='independent-proof')
        plan = self.preview()
        recovery_module.reserve_merge(self.root, self.profile, plan, reference=binding)
        peer = self.peer_store()
        original_git = GitRecoveryStore.git
        injected = False

        def racing_git(store, *args, **kwargs):
            nonlocal injected
            if args[0] == 'push' and not injected:
                injected = True
                peer.apply(peer.snapshot(), 'confirm', proof)
            return original_git(store, *args, **kwargs)

        with mock.patch.object(GitRecoveryStore, 'git', racing_git):
            with self.assertRaisesRegex(SyncwheelError, 'Git operation refused'):
                recovery_module.start_merge(self.root, plan, binding)
        state = peer.snapshot()['state']
        self.assertEqual(delivery_blockers(state, 'product'), ['late-proof'])
        self.assertEqual(state['operations']['merge-1']['phase'], 'prepared')

    def test_review_r3_confirm_before_effect_wins_across_clones(self):
        binding = self.binding()
        proof = self.attest('late-proof', 'incident', incident='late-proof', actor='responsible-agent',
                            delivery='product', proven=True, attributed=True, evidence='independent-proof')
        plan = self.preview()
        peer = self.peer_store()
        native_event = self.app.record_github_pr_merge_event

        def confirm_after_reservation(root, manifest_path, event_type, event_plan, **kwargs):
            result = native_event(root, manifest_path, event_type, event_plan, **kwargs)
            if event_type == 'github_pr_merge_prepared':
                peer.apply(peer.snapshot(), 'confirm', proof)
            return result

        with mock.patch.object(self.app, 'record_github_pr_merge_event', side_effect=confirm_after_reservation):
            with self.assertRaisesRegex(SyncwheelError, 'confirmed delivery hold'):
                self.apply_merge(plan, binding)
        self.assertEqual(delivery_blockers(peer.snapshot()['state'], 'product'), ['late-proof'])

    def test_review_r3_effect_start_wins_pending_proof_preserved_then_promoted(self):
        binding = self.binding()
        proof = self.attest('late-proof', 'incident', incident='late-proof', actor='responsible-agent',
                            delivery='product', proven=True, attributed=True, evidence='independent-proof')
        plan = self.preview()
        recovery_module.reserve_merge(self.root, self.profile, plan, reference=binding)
        recovery_module.start_merge(self.root, plan, binding)
        peer = self.peer_store()
        peer.apply(peer.snapshot(), 'confirm', proof)
        state = peer.snapshot()['state']
        self.assertEqual(delivery_blockers(state, 'product'), [])
        self.assertIn('late-proof', state['pending_incidents'])
        # Another work release is free while this effect is unresolved.
        other = self.attest('other-publication', 'publication', operation='other', actor='other-agent',
                            delivery='other-work', head=self.fixture.head)
        peer.apply(peer.snapshot(), 'prepare', other)
        peer.apply(peer.snapshot(), 'start', other)
        merged = self.fixture.observation(state='MERGED')
        merged['pr'].update(mergeCommit='f' * 40, mergedAt='2026-01-01T00:00:00Z')
        recovery_module.reconcile_reference(self.root, self.profile, binding, lambda request: merged)
        state = peer.snapshot()['state']
        self.assertEqual(delivery_blockers(state, 'product'), ['late-proof'])
        self.assertEqual(state['pending_incidents'], {})

    def test_review_r4_unmapped_target_real_merge_proceeds_without_actor_binding(self):
        state = self.store.snapshot()['state']
        state['policy']['product_refs'] = ['refs/heads/another-product']
        self.provision(self.state_ref, state)
        plan = self.preview(reference=None)
        self.assertEqual(plan['status'], 'ready')
        self.assertNotIn('actorRecovery', plan)
        self.assertEqual(self.apply_merge(plan, None), 0)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})

    def test_review_r4_newly_mapped_target_rejects_old_unscoped_preview(self):
        state = self.store.snapshot()['state']
        state['policy']['product_refs'] = ['refs/heads/another-product']
        self.provision(self.state_ref, state)
        plan = self.preview(reference=None)
        state = self.store.snapshot()['state']
        state['policy']['product_refs'] = ['refs/heads/main']
        self.provision(self.state_ref, state)
        with self.assertRaisesRegex(SyncwheelError, 'policy changed'):
            recovery_module.reserve_merge(self.root, {}, plan, reference=None)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})

    def fanout(self, first_name='unprotected', *, second=None):
        first = self.fixture.tmp / (first_name + '.git')
        subprocess.run(['git', 'init', '--bare', '-q', str(first)], check=True)
        self.store.git('push', str(first), self.fixture.base + ':refs/heads/main')
        alias = 'fanout-' + first_name
        self.fixture.git('remote', 'add', alias, str(first))
        self.fixture.git('config', '--add', 'remote.' + alias + '.pushurl', str(first))
        self.fixture.git('config', '--add', 'remote.' + alias + '.pushurl', str(second or self.fixture.remote))
        return alias, first

    def test_review_r5_second_pushurl_protected_no_destination_changes(self):
        alias, first = self.fanout()
        before = [self.store.git('--git-dir=' + str(path), 'rev-parse', 'refs/heads/main')
                  for path in (first, self.fixture.remote)]
        refusal = None
        try:
            self.app.run(['git', 'push', alias, self.fixture.head + ':refs/heads/main'], cwd=self.root)
        except SyncwheelError as exc:
            refusal = str(exc)
        after = [self.store.git('--git-dir=' + str(path), 'rev-parse', 'refs/heads/main')
                 for path in (first, self.fixture.remote)]
        self.assertEqual(before, after)
        self.assertIsNotNone(refusal)
        self.assertIn('product target', refusal)

    def test_review_r5_unrelated_work_ref_fans_out_normally(self):
        alias, first = self.fanout()
        ref = 'refs/heads/ordinary-work'
        self.app.run(['git', 'push', alias, self.fixture.head + ':' + ref], cwd=self.root)
        for path in (first, self.fixture.remote):
            self.assertEqual(self.store.git('--git-dir=' + str(path), 'rev-parse', ref), self.fixture.head)

    def test_review_r5_legacy_unprotected_destinations_keep_normal_publication(self):
        second = self.fixture.tmp / 'second-unprotected.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(second)], check=True)
        self.store.git('push', str(second), self.fixture.base + ':refs/heads/main')
        alias, first = self.fanout('legacy-first', second=second)
        self.app.run(['git', 'push', alias, self.fixture.head + ':refs/heads/main'], cwd=self.root)
        for path in (first, second):
            self.assertEqual(self.store.git('--git-dir=' + str(path), 'rev-parse', 'refs/heads/main'), self.fixture.head)

    def test_review_r6_explicit_url_git_resolution_matches_native_transport(self):
        for rewrite in ('insteadOf', 'pushInsteadOf'):
            with self.subTest(rewrite=rewrite):
                source = str(self.fixture.tmp / ('declared-alias-' + rewrite))
                self.fixture.git('config', 'url.' + str(self.fixture.remote) + '.' + rewrite, source)
                # Observe ordinary Git transport with a harmless work ref on a
                # local fixture. No bypass payload, external endpoint or product write.
                observed_ref = 'refs/heads/observed-' + rewrite.lower()
                self.store.git('push', source, self.fixture.head + ':' + observed_ref)
                self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote),
                                                'rev-parse', observed_ref), self.fixture.head)
                if rewrite == 'pushInsteadOf':
                    with self.assertRaisesRegex(SyncwheelError, 'requires a named remote'):
                        recovery_module.push_destinations(self.root, source)
                    alias = 'declared-rewrite-remote'
                    self.fixture.git('remote', 'add', alias, source)
                    self.assertEqual(recovery_module.push_destinations(self.root, alias), [str(self.fixture.remote)])
                    publication_destination = alias
                else:
                    self.assertEqual(recovery_module.push_destinations(self.root, source), [str(self.fixture.remote)])
                    publication_destination = source
                before = self.store.git('--git-dir=' + str(self.fixture.remote), 'rev-parse', 'refs/heads/main')
                with self.assertRaisesRegex(SyncwheelError, 'product target'):
                    self.app.run(['git', 'push', publication_destination, self.fixture.head + ':refs/heads/main'], cwd=self.root)
                self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote),
                                                'rev-parse', 'refs/heads/main'), before)
                allowed = 'refs/heads/allowed-' + rewrite.lower()
                self.app.run(['git', 'push', publication_destination, self.fixture.head + ':' + allowed], cwd=self.root)
                self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote),
                                                'rev-parse', allowed), self.fixture.head)

    def test_review_r6_inline_git_configuration_uses_same_resolution_as_push(self):
        source = str(self.fixture.tmp / 'inline-route')
        configuration = 'url.' + str(self.fixture.remote) + '.insteadOf=' + source
        prefix = ['git', '-c', configuration, 'push', source]
        ref = 'refs/heads/inline-work'
        self.app.run([*prefix, self.fixture.head + ':' + ref], cwd=self.root)
        self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote), 'rev-parse', ref), self.fixture.head)
        with self.assertRaisesRegex(SyncwheelError, 'product target'):
            self.app.run([*prefix, self.fixture.head + ':refs/heads/main'], cwd=self.root)
        self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote),
                                        'rev-parse', 'refs/heads/main'), self.fixture.base)

    def test_review_r6_relative_caller_directory_and_git_C_preserve_endpoint(self):
        peer = self.peer_store()
        selected = os.path.relpath(peer.root, self.root)
        destination = os.path.relpath(self.fixture.remote, peer.root)
        prefix = ['git', '-C', selected, 'push', destination]
        ref = 'refs/heads/relative-work'
        self.app.run([*prefix, self.fixture.head + ':' + ref], cwd=self.root)
        self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote), 'rev-parse', ref), self.fixture.head)
        with self.assertRaisesRegex(SyncwheelError, 'product target'):
            self.app.run([*prefix, self.fixture.head + ':refs/heads/main'], cwd=self.root)

    def test_review_r6_nonstable_rewrite_refuses_before_transport(self):
        first = str(self.fixture.tmp / 'first-declared-route')
        second = str(self.fixture.tmp / 'second-declared-route')
        self.fixture.git('config', 'url.' + second + '.insteadOf', first)
        self.fixture.git('config', 'url.' + str(self.fixture.remote) + '.insteadOf', second)
        before = self.store.git('--git-dir=' + str(self.fixture.remote), 'rev-parse', 'refs/heads/main')
        with self.assertRaisesRegex(SyncwheelError, 'not stable'):
            self.app.run(['git', 'push', first, self.fixture.head + ':refs/heads/main'], cwd=self.root)
        self.assertEqual(self.store.git('--git-dir=' + str(self.fixture.remote),
                                        'rev-parse', 'refs/heads/main'), before)


if __name__ == '__main__':
    unittest.main()
