"""Golden coverage for the porcelain replay seam.

The compressed fixture was captured from origin/main at 58c28cd before this
seam existed. Commands are invoked directly only to pin the backup timestamp;
the captured text is the stdout from stack rebuild --dry-run and int rebuild
--dry-run for each supported worktree location.
"""

import base64
import contextlib
import gzip
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = REPO_ROOT / 'tests' / 'fixtures' / 'replay-dry-run-golden.json.gz.b64'
BASELINE_ROOT = '/tmp/syncwheel-r3-baseline-origin-main'
FIXED_DATE = '2026-08-08T00:00:00+00:00'
FIXED_TIMESTAMP = '20260808T000000000000Z'


class ReplayExecutionSeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-replay-execution-seam-'))
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                'GIT_AUTHOR_DATE': FIXED_DATE,
                'GIT_COMMITTER_DATE': FIXED_DATE,
            },
        )
        self.environment_patch.start()
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Syncwheel Fixture')
        self.git('config', 'user.email', 'syncwheel@example.com')
        (self.repo / 'alpha.txt').write_text('alpha\n')
        self.git('add', 'alpha.txt')
        self.git('commit', '-q', '-m', 'feat: add alpha')
        alpha = self.git('rev-parse', '--short=7', 'HEAD')
        (self.repo / 'beta.txt').write_text('beta\n')
        self.git('add', 'beta.txt')
        self.git('commit', '-q', '-m', 'feat: add beta')
        beta = self.git('rev-parse', '--short=7', 'HEAD')
        self.git('branch', 'pr/feature-a', 'HEAD~1')
        self.git('branch', 'pr/feature-b', 'HEAD')
        self.manifest = {
            'version': 1,
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'fork',
                'base_branch': 'main',
                'base_ref': 'main',
            },
            'integration': {
                'branch': 'main',
                'base': 'main',
                'stacks': ['feature-a', 'feature-b'],
            },
            'stacks': [
                {
                    'id': 'feature-a',
                    'branch': 'pr/feature-a',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha],
                },
                {
                    'id': 'feature-b',
                    'branch': 'pr/feature-b',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha, beta],
                },
            ],
        }
        self.write_manifest()
        self.module = self.load_module()
        self.module.syncwheel_timestamp = lambda: FIXED_TIMESTAMP

    def tearDown(self):
        self.environment_patch.stop()
        shutil.rmtree(self.tmp)

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ['git', *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            self.fail(f'git {args} failed:\n{result.stdout}\n{result.stderr}')
        return result.stdout.strip()

    def load_module(self):
        import importlib.util

        cli = REPO_ROOT / 'scripts' / 'syncwheel.py'
        spec = importlib.util.spec_from_file_location('syncwheel_replay_execution_seam', cli)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write_manifest(self):
        path = self.repo / '.syncwheel' / 'manifest.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(self.manifest, indent=2) + '\n')

    def args(self, **values):
        return SimpleNamespace(
            repo=str(self.repo),
            manifest=None,
            personal=None,
            dry_run=True,
            **values,
        )

    def capture(self, command, args):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = command(args)
        self.assertEqual(status, 0)
        return stream.getvalue()

    def golden_outputs(self):
        encoded = GOLDEN_PATH.read_text().strip()
        return json.loads(gzip.decompress(base64.b64decode(encoded)))

    def test_dry_run_output_is_byte_identical_to_origin_main_golden(self):
        outputs = {}
        outputs['stack_new_worktree'] = self.capture(
            self.module.command_stack_rebuild,
            self.args(stack='feature-a', worktree=str(self.tmp / 'stack-new'), in_place=False),
        )
        stack_existing = self.tmp / 'stack-existing'
        self.git('worktree', 'add', '-q', str(stack_existing), 'pr/feature-a')
        outputs['stack_matching_worktree'] = self.capture(
            self.module.command_stack_rebuild,
            self.args(stack='feature-a', worktree=None, in_place=False),
        )
        outputs['stack_in_place'] = self.capture(
            self.module.command_stack_rebuild,
            self.args(stack='feature-a', worktree=None, in_place=True),
        )

        self.git('branch', 'integration/new', 'main')
        self.manifest['integration']['branch'] = 'integration/new'
        self.write_manifest()
        outputs['integration_new_worktree'] = self.capture(
            self.module.command_int_rebuild,
            self.args(worktree=str(self.tmp / 'integration-new'), in_place=False),
        )
        self.git('branch', 'integration/existing', 'main')
        integration_existing = self.tmp / 'integration-existing'
        self.git('worktree', 'add', '-q', str(integration_existing), 'integration/existing')
        self.manifest['integration']['branch'] = 'integration/existing'
        self.write_manifest()
        outputs['integration_matching_worktree'] = self.capture(
            self.module.command_int_rebuild,
            self.args(worktree=None, in_place=False),
        )
        self.manifest['integration']['branch'] = 'main'
        self.write_manifest()
        outputs['integration_in_place'] = self.capture(
            self.module.command_int_rebuild,
            self.args(worktree=None, in_place=True),
        )

        expected = {
            key: value.replace(BASELINE_ROOT, str(self.tmp))
            for key, value in self.golden_outputs().items()
        }
        self.assertEqual(outputs, expected)

    def test_non_plumbing_step_render_is_quoted_argv(self):
        stack = self.manifest['stacks'][0]
        plan = self.module.replay_plan(
            self.repo,
            self.manifest,
            self.module.replay_target(stack=stack, worktree=self.tmp / 'render'),
            'desk',
        )

        for step in plan['steps']:
            with self.subTest(argv=step['argv']):
                self.assertEqual(step['kind'], 'exec')
                self.assertEqual(step['render'], self.module.quoted(step['argv']))

    def test_stack_projection_skips_a_declared_base_commit(self):
        stack = dict(self.manifest['stacks'][0])
        stack['commits'] = [self.git('rev-parse', 'main')]

        self.assertEqual(
            self.module.materialize_stack_projection(self.repo, stack),
            self.module.ref_tree(self.repo, 'main'),
        )


if __name__ == '__main__':
    unittest.main()
