#!/usr/bin/env python3
"""Contract tests for deterministic unittest shard assignment."""

from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("run-unittest-shard.py")


class SyntheticCase:
    def __init__(self, test_id: str):
        self.test_id = test_id

    def id(self) -> str:
        return self.test_id


def load_runner():
    spec = importlib.util.spec_from_file_location("run_unittest_shard", RUNNER_PATH)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


class ShardAssignmentContractTest(unittest.TestCase):
    def test_assignment_is_deterministic_complete_and_exactly_once(self):
        runner = load_runner()

        weights = {
            "synthetic.Case.test_slow": 9.0,
            "synthetic.Case.test_medium": 4.0,
            "synthetic.Case.test_quick": 2.0,
        }
        test_ids = [
            "synthetic.Case.test_slow",
            "synthetic.Case.test_medium",
            "synthetic.Case.test_quick",
            "synthetic.Case.test_without_weight",
        ]
        self.assertNotIn(test_ids[-1], weights)

        with tempfile.TemporaryDirectory() as temporary_directory:
            weights_path = Path(temporary_directory) / "weights.json"
            weights_path.write_text(json.dumps({"weights": weights}))
            runner.WEIGHTS_PATH = weights_path

            def assignment(ids):
                shards = runner.shard_cases([SyntheticCase(test_id) for test_id in ids], 3)
                return [[case.id() for case in shard] for shard in shards]

            expected = [
                ["synthetic.Case.test_slow"],
                ["synthetic.Case.test_medium"],
                [
                    "synthetic.Case.test_quick",
                    "synthetic.Case.test_without_weight",
                ],
            ]
            self.assertEqual(assignment(test_ids), expected)
            self.assertEqual(assignment(list(reversed(test_ids))), expected)

        assigned = [test_id for shard in expected for test_id in shard]
        self.assertEqual(Counter(assigned), Counter(test_ids))
        self.assertTrue(all(count == 1 for count in Counter(assigned).values()))

    def test_timed_result_records_every_test(self):
        runner = load_runner()

        class Sample(unittest.TestCase):
            def test_passes(self):
                pass

            def test_fails(self):
                self.fail("expected")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Sample)
        test_ids = sorted(case.id() for case in suite)
        unittest.TextTestRunner(
            stream=io.StringIO(), resultclass=runner.TimedTextTestResult
        ).run(suite)
        self.assertEqual(sorted(runner.MEASURED_SECONDS), test_ids)
        self.assertTrue(all(seconds >= 0 for seconds in runner.MEASURED_SECONDS.values()))


if __name__ == "__main__":
    unittest.main()
