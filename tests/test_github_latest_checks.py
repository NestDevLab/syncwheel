import json
import unittest
from unittest import mock

import test_github_pr_merge as fixtures


class GithubLatestChecksTest(unittest.TestCase):
    repository = 'example/project'
    actor = 'fixture-author'

    def setUp(self):
        self.fixture = fixtures.GithubPrMergeTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.github = fixtures.ADAPTER
        self.syncwheel = fixtures.SYNCWHEEL
        self.fixture.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.fixture.manifest['stacks'][0]['github'] = {'pr': 25}
        self.fixture.manifest_path.write_text(json.dumps(self.fixture.manifest) + '\n')

    def check(self, run, conclusion='SUCCESS', *, workflow='build', created='2026-01-01T00:00:00Z',
              attempt=1, status='COMPLETED', head=None, number=None, name='ci', job_index=0):
        head = head or self.fixture.head
        job_id = run * 1000 + attempt * 100 + job_index
        check_id = job_id + 100_000
        node_id = f'CR_fixture_{check_id}'
        url = f'https://github.com/{self.repository}/actions/runs/{run}/job/{job_id}'
        return {
            'node_id': node_id,
            'rollup': {'__typename': 'CheckRun', 'name': name, 'workflowName': workflow,
                       'status': status, 'conclusion': conclusion, 'startedAt': created,
                       'completedAt': created if status == 'COMPLETED' else None, 'detailsUrl': url},
            'run': {'id': run, 'head_sha': head, 'check_suite_id': run + 1_000,
                    'workflow_id': {'build': 10, 'lint': 11, 'test': 12}[workflow],
                    'run_number': number if number is not None else run,
                    'run_attempt': attempt, 'created_at': created},
            'check': {'id': check_id, 'node_id': node_id, 'name': name, 'head_sha': head, 'status': status.lower(),
                      'conclusion': conclusion.lower() or None, 'details_url': url,
                      'check_suite': {'id': run + 1_000}, 'app': {'id': 20, 'slug': 'github-actions'}},
            'job': {'id': job_id, 'name': name, 'run_id': run, 'head_sha': head,
                    'check_run_url': f'https://api.github.com/repos/{self.repository}/check-runs/{check_id}'},
        }

    def status_context(self, name, state):
        return {'__typename': 'StatusContext', 'id': f'SC_{name}', 'context': name, 'state': state}

    def graph_commit(self, contexts, total, *, cursor=None, head=None):
        return {
            'oid': head or self.fixture.head,
            'statusCheckRollup': {'contexts': {
                'nodes': contexts,
                'totalCount': total,
                'pageInfo': {'hasNextPage': cursor is not None, 'endCursor': cursor},
            }},
        }

    def plan(self, records, *, required=False, checks='all', pages=None, graphql_head=None,
             graphql_truncated=False, legacy=(), graph_pages=None, transport=None):
        policy = self.fixture.policy()
        policy.update(repository=self.repository, head_repositories=[self.repository], checks=checks,
                      merge_actors=[self.actor], pr_authors=[self.actor], commit_authors=[self.actor])
        profile = {'hooks': {'mode': 'required'}, 'github_pr_merge': policy}
        (self.fixture.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        pull = {
            'number': 25, 'state': 'OPEN', 'isDraft': False,
            'headRefName': 'pr/feature', 'headRefOid': self.fixture.head,
            'baseRefName': 'main', 'baseRefOid': self.fixture.base,
            'headRepository': {'nameWithOwner': self.repository}, 'author': {'login': self.actor},
            'commits': [{'oid': self.fixture.head, 'authors': [{'login': self.actor}]}],
            'reviewDecision': 'APPROVED', 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN',
            'statusCheckRollup': [*[row['rollup'] for row in records], *legacy],
        }

        prefix = f'repos/{self.repository}'
        runs = {}
        for row in records:
            run = row['run']
            if run['id'] not in runs or run['run_attempt'] > runs[run['id']]['run_attempt']:
                runs[run['id']] = run

        def page(key, rows, number=1):
            overrides = pages or {}
            if (key, number) in overrides:
                response = overrides[key, number]
                if isinstance(response, Exception):
                    raise response
                return response
            self.assertEqual(number, 1, f'unexpected {key} page')
            return {'total_count': len(rows), key: rows}

        def observe(cwd, request):
            try:
                return self.github.observe(request, cwd)
            except self.github.AdapterError as exc:
                raise self.syncwheel.SyncwheelError(str(exc)) from exc

        def run_json(argv, cwd):
            if argv == ['api', f'repos/{self.repository}']:
                value = {'id': 1000, 'full_name': self.repository, 'permissions': {'admin': True},
                         'clone_url': str(transport if transport is not None else self.fixture.remote),
                         'allow_squash_merge': True}
            elif argv == ['api', 'user']:
                value = {'login': self.actor}
            elif argv[:3] == ['pr', 'view', '25']:
                value = pull
            elif f'query={self.github.THREAD_QUERY}' in argv:
                if graph_pages is not None:
                    commit = graph_pages[None]
                else:
                    contexts = [{'id': row['node_id'], **row['rollup']} for row in records]
                    contexts.extend(legacy)
                    commit = self.graph_commit(contexts, len(contexts), head=graphql_head)
                    if graphql_truncated:
                        commit['statusCheckRollup']['contexts']['pageInfo']['hasNextPage'] = True
                value = {'data': {'repository': {'pullRequest': {
                    'reviewThreads': {'nodes': [], 'pageInfo': {'hasNextPage': False}},
                    'commits': {'nodes': [{'commit': commit}]},
                }}}}
            elif getattr(self.github, 'CHECK_PAGE_QUERY', None) and f'query={self.github.CHECK_PAGE_QUERY}' in argv:
                self.assertIn(f'head={self.fixture.head}', argv)
                cursor = next(arg.split('=', 1)[1] for arg in argv if arg.startswith('cursor='))
                commit = graph_pages[cursor]
                if isinstance(commit, Exception):
                    raise commit
                value = {'data': {'repository': {'object': commit}}}
            elif len(argv) == 2 and argv[1].startswith(
                f'{prefix}/commits/{self.fixture.head}/check-runs?filter=all&per_page=100&page='
            ):
                value = page('check_runs', [row['check'] for row in records], int(argv[1].rsplit('=', 1)[1]))
            elif len(argv) == 2 and argv[1].startswith(
                f'{prefix}/actions/runs?head_sha={self.fixture.head}&per_page=100&page='
            ):
                value = page('workflow_runs', list(runs.values()), int(argv[1].rsplit('=', 1)[1]))
            elif len(argv) == 2 and argv[0] == 'api' and '/attempts/' in argv[1]:
                value = page('jobs', [
                    row['job'] for row in records if argv[1] == (
                        f"{prefix}/actions/runs/{row['run']['id']}/"
                        f"attempts/{row['run']['run_attempt']}/jobs?per_page=100&page=1"
                    )
                ])
            else:
                self.fail(f'unexpected GitHub observation: {argv}')
            return {'ok': True, 'value': value}

        def observe_rule(*, repo, path, cwd):
            value = [] if '/rules/' in path else {}
            if required and isinstance(value, dict):
                value['required_status_checks'] = {'contexts': ['ci']}
            return {'status': 200, 'value': value, 'detail': None}

        with (
            mock.patch.object(self.github, 'run_json', side_effect=run_json),
            mock.patch.object(self.github, 'observe_acceptance_authority', return_value=dict(
                self.fixture.acceptance_authority(), repository={'id': 1000, 'full_name': self.repository})),
            mock.patch.object(self.github, '_optional_api', side_effect=observe_rule),
            mock.patch.object(self.syncwheel, 'github_adapter_request', side_effect=observe),
            mock.patch.object(self.syncwheel, 'validate_manifest', return_value={'errors': []}),
        ):
            return self.syncwheel.build_github_pr_merge_plan(
                self.fixture.repo, self.fixture.manifest, self.fixture.manifest_path, 'feature',
                self.fixture.args(),
            )

    def test_absent_policy_is_proved_on_the_real_public_fixture_transport(self):
        self.assertEqual(self.fixture.git('ls-remote', '--refs', 'origin',
                                         'refs/heads/syncwheel/actor-recovery'), '')
        plan = self.plan([self.check(101)], required=True)
        self.assertEqual(plan['recoveryRemote'], str(self.fixture.remote))
        self.assertEqual(plan['status'], 'ready', plan['blockers'])
        self.assertNotIn('actorRecovery', plan)

    def test_unavailable_policy_discovery_is_not_treated_as_absent(self):
        before = self.fixture.git('ls-remote', '--refs', 'origin')
        local_refs = self.fixture.git('for-each-ref', '--format=%(refname) %(objectname)')
        unavailable = self.fixture.tmp / 'unavailable-public-mirror.git'
        plan = self.plan([self.check(101)], required=True, transport=unavailable)
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['actorRecovery']['status'], 'blocked')
        self.assertIsNone(plan['actorRecovery']['policy'])
        self.assertIn('Git operation refused', plan['actorRecovery']['detail'])
        self.assertIn({'code': 'attributed_delivery_recovery',
                       'detail': plan['actorRecovery']['detail']}, plan['blockers'])
        self.assertEqual(self.fixture.git('ls-remote', '--refs', 'origin'), before)
        self.assertEqual(self.fixture.git('for-each-ref', '--format=%(refname) %(objectname)'), local_refs)

    def test_new_success_supersedes_old_cancelled_regardless_of_input_order(self):
        old = self.check(101, 'CANCELLED')
        new = self.check(102, created='2026-01-01T01:00:00Z')
        new['rollup']['workflowName'] = 'updated display name'
        effective_only = self.plan([new], required=True)
        for records in ([old, new], [new, old]):
            with self.subTest(old_first=records[0] is old):
                plan = self.plan(records, required=True)
                self.assertEqual(plan['status'], 'ready', plan['blockers'])
                self.assertEqual([row['checkRunId'] for row in plan['checks']], [new['check']['id']])
                self.assertEqual([row['checkRunId'] for row in plan['supersededChecks']], [old['check']['id']])
                self.assertEqual(plan['supersededChecks'][0]['conclusion'], 'CANCELLED')
                self.assertEqual(plan['planDigest'], effective_only['planDigest'])

    def test_latest_pending_failure_or_cancellation_remains_blocking(self):
        old = self.check(101)
        for status, conclusion in [('QUEUED', ''), ('COMPLETED', 'FAILURE'), ('COMPLETED', 'CANCELLED')]:
            new = self.check(102, conclusion, status=status, created='2026-01-01T01:00:00Z')
            for checks, code in [('none', 'required_check_failed_or_pending'), ('all', 'check_failed_or_pending')]:
                with self.subTest(status=status, conclusion=conclusion, checks=checks):
                    plan = self.plan([new, old], required=True, checks=checks)
                    self.assertEqual(plan['status'], 'blocked')
                    self.assertIn(code, {row['code'] for row in plan['blockers']})

    def test_same_name_across_workflows_remains_independent_for_required_check(self):
        failed = self.check(101, 'FAILURE', workflow='lint')
        passed = self.check(102, workflow='test', created='2026-01-01T01:00:00Z')
        failed['rollup']['workflowName'] = passed['rollup']['workflowName'] = 'shared display name'
        for rollup in ([failed, passed], [passed, failed]):
            with self.subTest(rollup=rollup):
                plan = self.plan(rollup, required=True, checks='none')
                self.assertEqual(plan['status'], 'blocked')
                self.assertIn('required_check_failed_or_pending', {row['code'] for row in plan['blockers']})

    def test_success_for_a_different_head_cannot_satisfy_current_required_check(self):
        wrong_head = '7' * 40
        stale = self.check(100, head=wrong_head)
        plan = self.plan([stale], required=True, checks='none')

        self.assertEqual(plan['status'], 'blocked')

    def test_stale_or_truncated_graphql_check_rollup_cannot_authorize_merge(self):
        for kwargs in [{'graphql_head': '7' * 40}, {'graphql_truncated': True}]:
            with self.subTest(**kwargs):
                plan = self.plan([self.check(101)], required=True, **kwargs)

                self.assertEqual(plan['status'], 'blocked')
                self.assertIn('checks_observation_incomplete', {row['code'] for row in plan['blockers']})

    def test_distinct_run_numbers_resolve_equal_creation_times(self):
        failed = self.check(101, 'FAILURE')
        passed = self.check(102)

        plan = self.plan([passed, failed], required=True)

        self.assertEqual(plan['status'], 'ready', plan['blockers'])
        self.assertEqual([row['checkRunId'] for row in plan['checks']], [passed['check']['id']])

    def test_missing_creation_time_or_conflicting_run_numbers_keep_failure(self):
        for created, number in [(None, 102), ('2026-01-01T00:00:00Z', 101)]:
            with self.subTest(created=created, number=number):
                failed = self.check(101, 'FAILURE')
                passed = self.check(102, created=created, number=number)
                plan = self.plan([failed, passed], required=True)

                self.assertEqual(plan['status'], 'blocked')

    def test_higher_attempt_of_same_run_wins_when_timestamps_are_equal(self):
        old = self.check(101, 'CANCELLED', attempt=1)
        new = self.check(101, attempt=2)
        for rollup in ([old, new], [new, old]):
            with self.subTest(rollup=rollup):
                plan = self.plan(rollup, required=True)

                self.assertEqual(plan['status'], 'ready', plan['blockers'])

    def test_unmatched_rollup_failure_cannot_be_discarded(self):
        failed = self.check(101, 'FAILURE')
        passed = self.check(102, created='2026-01-01T01:00:00Z')
        failed['node_id'] = 'CR_unmatched'

        plan = self.plan([failed, passed], required=True)

        self.assertEqual(plan['status'], 'blocked')

    def test_incomplete_check_run_enrichment_does_not_authorize_merge(self):
        records = [self.check(run) for run in range(1, 101)]
        first_page = [row['check'] for row in records]
        for count, second in [
            (10_000, None),
            (101, self.github.AdapterError('fixture pagination error')),
            (101, {'total_count': 102, 'check_runs': [self.check(101)['check']]}),
            (101, {'total_count': 101, 'check_runs': []}),
        ]:
            with self.subTest(count=count, second=second):
                pages = {('check_runs', 1): {'total_count': count, 'check_runs': first_page}}
                if second is not None:
                    pages['check_runs', 2] = second

                plan = self.plan(records, required=True, pages=pages)

                self.assertEqual(plan['status'], 'blocked')

    def test_actions_server_cap_cannot_authorize_merge_with_green_observed_checks(self):
        records = [self.check(run) for run in range(1, 1001)]
        pages = {
            ('workflow_runs', page): {
                'total_count': 1000,
                'workflow_runs': [row['run'] for row in records[(page - 1) * 100:page * 100]],
            }
            for page in range(1, 11)
        }

        plan = self.plan(records[:1], required=True, pages=pages)

        self.assertEqual(plan['status'], 'blocked')

    def test_ambiguous_rollup_to_rest_match_cannot_discard_rollup_failure(self):
        first = self.check(101)
        second = self.check(102)
        first['rollup']['conclusion'] = 'FAILURE'
        first['node_id'] = 'CR_unmatched'
        second['check']['details_url'] = first['check']['details_url']
        second['rollup']['detailsUrl'] = first['rollup']['detailsUrl']

        plan = self.plan([first, second], required=True)

        self.assertEqual(plan['status'], 'blocked')

    def test_matching_check_with_conflicting_rollup_and_rest_outcomes_blocks(self):
        record = self.check(101)
        record['rollup']['conclusion'] = 'FAILURE'

        plan = self.plan([record], required=True)

        self.assertEqual(plan['status'], 'blocked')

    def test_partial_rerun_preserves_last_successful_attempt_for_other_jobs(self):
        existing = self.check(101, name='independent', job_index=1)
        failed = self.check(101, 'FAILURE', attempt=1)
        passed = self.check(101, attempt=2)

        plan = self.plan([passed, existing, failed], required=True)

        self.assertEqual(plan['status'], 'ready', plan['blockers'])
        self.assertIn('independent', {row['name'] for row in plan['checks']})

    def test_two_distinct_checks_in_same_attempt_remain_ambiguous(self):
        old = self.check(101, 'CANCELLED', attempt=1)
        first = self.check(101, attempt=2)
        second = self.check(101, attempt=2, job_index=1)

        plan = self.plan([old, first, second], required=True, checks='none')

        self.assertEqual(plan['status'], 'blocked')

    def test_attempt_job_must_prove_check_run_identity_and_exact_head(self):
        wrong_link = f'https://api.github.com/repos/{self.repository}/check-runs/999'
        for field, value in [('run_id', 999), ('head_sha', '7' * 40), ('check_run_url', wrong_link)]:
            with self.subTest(field=field):
                old = self.check(101, 'CANCELLED', attempt=1)
                new = self.check(101, attempt=2)
                new['job'][field] = value

                plan = self.plan([old, new], required=True)

                self.assertEqual(plan['status'], 'blocked')

    def test_same_head_legacy_status_change_between_observations_blocks(self):
        prior = self.status_context('ci', 'SUCCESS')
        current = self.status_context('ci', 'FAILURE')
        pages = {None: self.graph_commit([current], 1)}

        plan = self.plan([], legacy=[prior], graph_pages=pages, required=True)

        self.assertEqual(plan['status'], 'blocked')
        codes = {row['code'] for row in plan['blockers']}
        self.assertTrue(codes & {'checks_observation_incomplete', 'check_failed_or_pending'})

    def test_complete_graphql_pagination_includes_check_beyond_first_hundred(self):
        first = [self.status_context(f'ci-{index}', 'SUCCESS') for index in range(100)]
        for state in ('SUCCESS', 'FAILURE'):
            with self.subTest(state=state):
                last = self.status_context('ci-last', state)
                pages = {
                    None: self.graph_commit(first, 101, cursor='page-2'),
                    'page-2': self.graph_commit([last], 101),
                }

                plan = self.plan([], legacy=first, graph_pages=pages)

                self.assertEqual(plan['status'], 'ready' if state == 'SUCCESS' else 'blocked')
                self.assertIn('ci-last', {row['name'] for row in plan['checks']})
                self.assertNotIn('checks_observation_incomplete', {row['code'] for row in plan['blockers']})
                if state == 'FAILURE':
                    self.assertTrue(any(row['code'] == 'check_failed_or_pending' and 'ci-last' in row['detail']
                                        for row in plan['blockers']))

    def test_graphql_continuation_errors_or_moves_to_other_head_fail_closed(self):
        first = [self.status_context(f'ci-{index}', 'SUCCESS') for index in range(100)]
        last = self.status_context('ci-last', 'SUCCESS')
        for continuation in [
            self.github.AdapterError('fixture continuation failed'),
            self.graph_commit([last], 101, head='7' * 40),
            self.graph_commit([], 101),
        ]:
            with self.subTest(continuation=continuation):
                pages = {None: self.graph_commit(first, 101, cursor='page-2'), 'page-2': continuation}

                plan = self.plan([], legacy=first, graph_pages=pages)

                self.assertEqual(plan['status'], 'blocked')
                self.assertIn('checks_observation_incomplete', {row['code'] for row in plan['blockers']})

    def test_graphql_page_bound_fails_closed_with_green_observed_checks(self):
        for total, page_size in [(1001, 100), (1000, 99)]:
            with self.subTest(total=total, page_size=page_size):
                nodes = [self.status_context(f'ci-{index}', 'SUCCESS') for index in range(10 * page_size)]
                pages = {
                    None if index == 0 else f'page-{index + 1}': self.graph_commit(
                        nodes[index * page_size:(index + 1) * page_size], total, cursor=f'page-{index + 2}',
                    )
                    for index in range(10)
                }

                plan = self.plan([], legacy=nodes[:page_size], graph_pages=pages)

                self.assertEqual(plan['status'], 'blocked')
                self.assertIn('checks_observation_incomplete', {row['code'] for row in plan['blockers']})
