import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'
FIXTURE = REPO_ROOT / 'tests' / 'fixtures' / 'simple-repo'


class SyncwheelFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-test-'))
        self.repo = self.tmp / 'repo'
        shutil.copytree(FIXTURE, self.repo)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, expected=0):
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def test_validate_passes_for_fixture(self):
        result = self.run_cli('validate', expected=0)
        self.assertIn('OK', result.stdout)

    def test_plan_reports_no_actions_when_fixture_is_aligned(self):
        result = self.run_cli('plan', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertEqual(data, [])

    def test_status_json_reports_manifest_present(self):
        result = self.run_cli('status', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])
        self.assertIn('validation', data)
        self.assertEqual(data['validation']['errors'], [])

    def test_materialize_commands_are_emitted(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli('materialize-pr', 'feature-a', '--worktree', str(worktree), expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git worktree add -B pr/feature-a', result.stdout)
        self.assertIn('git -C', result.stdout)

    def test_validate_fails_when_commit_is_missing(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['stacks'][0]['commits'].append('deadbeef')
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        result = self.run_cli('validate', expected=1)
        self.assertIn('missing commit', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
