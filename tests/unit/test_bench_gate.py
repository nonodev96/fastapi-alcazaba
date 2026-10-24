import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "bench_gate.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_runs(tmp_path: Path, previous: dict[str, float], current: dict[str, float]) -> list[str]:
    history = {
        "entries": {
            "Benchmark": [{"benches": [{"name": n, "value": v} for n, v in previous.items()]}]
        }
    }
    (tmp_path / "data.json").write_text(json.dumps(history))
    run = {"benchmarks": [{"fullname": n, "stats": {"ops": v}} for n, v in current.items()]}
    (tmp_path / "benchmarks.json").write_text(json.dumps(run))
    return ["bench_gate", str(tmp_path / "benchmarks.json"), str(tmp_path / "data.json")]


def test_a_slower_machine_is_not_a_regression(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every benchmark at roughly half the speed: the runner, not the code.
    previous = {"hit": 11708.0, "miss": 16164.0, "encode": 215326.0, "sign": 101343.0}
    current = {"hit": 5744.0, "miss": 8783.0, "encode": 121310.0, "sign": 52304.0}
    monkeypatch.setattr("sys.argv", write_runs(tmp_path, previous, current))
    assert gate.main() == 0


def test_one_benchmark_slower_than_the_rest_fails(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"hit": 10000.0, "miss": 10000.0, "encode": 10000.0, "sign": 10000.0}
    current = {"hit": 4000.0, "miss": 10000.0, "encode": 9500.0, "sign": 10500.0}
    monkeypatch.setattr("sys.argv", write_runs(tmp_path, previous, current))
    assert gate.main() == 1


def test_new_and_removed_benchmarks_are_ignored(gate: ModuleType) -> None:
    relative = gate.relative_slowdowns({"old": 1.0, "kept": 2.0}, {"kept": 2.0, "new": 1.0})
    assert relative == {"kept": 1.0}


def test_without_a_previous_run_it_passes(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "benchmarks.json").write_text(json.dumps({"benchmarks": []}))
    missing = tmp_path / "benchmark-history" / "data.json"
    monkeypatch.setattr("sys.argv", ["bench_gate", str(tmp_path / "benchmarks.json"), str(missing)])
    assert gate.main() == 0
