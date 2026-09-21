#!/usr/bin/env python3
"""Rebuild test-duration-weights.json from run-unittest-shard.py --durations files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


WEIGHTS_PATH = Path(__file__).with_name("test-duration-weights.json")
MINIMUM_SECONDS = 1.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("durations", type=Path, nargs="+")
    parser.add_argument("--source", required=True, help="where the durations were measured")
    args = parser.parse_args()

    durations: dict[str, float] = {}
    for path in args.durations:
        durations.update(json.loads(path.read_text()))
    weights = {
        test_id: round(seconds, 1)
        for test_id, seconds in sorted(durations.items())
        if seconds >= MINIMUM_SECONDS
    }
    payload = {"source": args.source, "minimum_seconds": MINIMUM_SECONDS, "weights": weights}
    WEIGHTS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {len(weights)} weights from {len(durations)} measured tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
