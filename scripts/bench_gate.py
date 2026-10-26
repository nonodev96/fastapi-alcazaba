"""Fail when a benchmark got slower than the others since main's last run.

    pytest tests/benchmarks --benchmark-only --benchmark-json=benchmarks.json
    python scripts/bench_gate.py benchmarks.json benchmark-history/data.json

GitHub's shared runners change from one run to the next, and a slower machine makes every
benchmark slower at once (twice as slow is common), so the absolute numbers of two runs can't be
compared. Each benchmark's slowdown is divided by the median slowdown of all of them: the machine
cancels out and what is left is what the code changed. A change that slows every benchmark by the
same amount goes unnoticed; the job summary still shows the absolute numbers.

The previous run is the last entry that benchmark-action/github-action-benchmark saved in its
data file (`external-data-json-path`). Without one (the first run, or an expired cache) it passes.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

# The `name` input of github-action-benchmark, which keys its entries.
SUITE = "Benchmark"


def current_results(path: Path) -> dict[str, float]:
    """Operations per second of each benchmark in a pytest-benchmark JSON file."""
    data = json.loads(path.read_text())
    return {bench["fullname"]: float(bench["stats"]["ops"]) for bench in data["benchmarks"]}


def previous_results(path: Path) -> dict[str, float] | None:
    """Operations per second of the last run saved by github-action-benchmark, if there is one."""
    if not path.exists():
        return None
    entries = json.loads(path.read_text()).get("entries", {}).get(SUITE) or []
    if not entries:
        return None
    return {bench["name"]: float(bench["value"]) for bench in entries[-1]["benches"]}


def relative_slowdowns(previous: dict[str, float], current: dict[str, float]) -> dict[str, float]:
    """How much slower each benchmark got, compared with the median of all of them (1 = as much)."""
    slowdowns = {name: previous[name] / current[name] for name in previous.keys() & current.keys()}
    if not slowdowns:
        return {}
    machine = statistics.median(slowdowns.values())
    return {name: slowdown / machine for name, slowdown in slowdowns.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("current", type=Path, help="pytest-benchmark JSON of this run")
    parser.add_argument("history", type=Path, help="github-action-benchmark data file")
    parser.add_argument("--threshold", type=float, default=1.5, help="relative slowdown that fails")
    args = parser.parse_args()

    previous = previous_results(args.history)
    if previous is None:
        print("No previous run to compare with.")
        return 0
    relative = relative_slowdowns(previous, current_results(args.current))
    for name, ratio in sorted(relative.items(), key=lambda item: item[1], reverse=True):
        print(f"{ratio:6.2f}x  {name}")
    regressions = [name for name, ratio in relative.items() if ratio > args.threshold]
    if regressions:
        print(f"{len(regressions)} benchmark(s) more than {args.threshold}x slower than the rest.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
