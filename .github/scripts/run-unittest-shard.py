#!/usr/bin/env python3
"""Discover the unittest suite and run one deterministic shard."""

from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from collections.abc import Iterator
from pathlib import Path


DEFAULT_WEIGHT_SECONDS = 1.0
WEIGHTS_PATH = Path(__file__).with_name("test-duration-weights.json")


def iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--list", action="store_true", dest="list_only")
    parser.add_argument(
        "--durations",
        type=Path,
        help="write each test's wall time in seconds to this JSON file",
    )
    args = parser.parse_args()
    if args.shard_count < 1:
        parser.error("--shard-count must be positive")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be between 0 and shard-count - 1")
    return args


def shard_cases(
    cases: list[unittest.TestCase],
    shard_count: int,
) -> list[list[unittest.TestCase]]:
    weights = json.loads(WEIGHTS_PATH.read_text())["weights"]
    shards: list[list[unittest.TestCase]] = [[] for _ in range(shard_count)]
    loads = [0.0] * shard_count

    weighted_cases = sorted(
        cases,
        key=lambda case: (-weights.get(case.id(), DEFAULT_WEIGHT_SECONDS), case.id()),
    )
    for case in weighted_cases:
        shard_index = min(range(shard_count), key=lambda index: (loads[index], index))
        shards[shard_index].append(case)
        loads[shard_index] += weights.get(case.id(), DEFAULT_WEIGHT_SECONDS)

    for shard in shards:
        shard.sort(key=lambda case: case.id())
    return shards


class TimedTextTestResult(unittest.TextTestResult):
    durations: dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        self._started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.durations[test.id()] = time.perf_counter() - self._started
        super().stopTest(test)


def main() -> int:
    args = parse_args()
    discovered = unittest.defaultTestLoader.discover("tests")
    cases = list(iter_cases(discovered))
    selected = shard_cases(cases, args.shard_count)[args.shard_index]

    if args.list_only:
        for case in selected:
            print(case.id())
        return 0

    print(
        f"Running shard {args.shard_index + 1}/{args.shard_count}: "
        f"{len(selected)} of {len(cases)} tests",
        flush=True,
    )
    result = unittest.TextTestRunner(
        verbosity=2, resultclass=TimedTextTestResult
    ).run(unittest.TestSuite(selected))
    if args.durations:
        args.durations.write_text(
            json.dumps(TimedTextTestResult.durations, indent=2, sort_keys=True) + "\n"
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
