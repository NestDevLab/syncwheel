#!/usr/bin/env python3
"""Discover the unittest suite and run one deterministic shard."""

from __future__ import annotations

import argparse
import sys
import unittest
from collections.abc import Iterator


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
    args = parser.parse_args()
    if args.shard_count < 1:
        parser.error("--shard-count must be positive")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be between 0 and shard-count - 1")
    return args


def main() -> int:
    args = parse_args()
    discovered = unittest.defaultTestLoader.discover("tests")
    cases = sorted(iter_cases(discovered), key=lambda case: case.id())
    selected = cases[args.shard_index :: args.shard_count]

    if args.list_only:
        for case in selected:
            print(case.id())
        return 0

    print(
        f"Running shard {args.shard_index + 1}/{args.shard_count}: "
        f"{len(selected)} of {len(cases)} tests",
        flush=True,
    )
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
