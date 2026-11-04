"""Smoke test of an installed fastapi-alcazaba (a built wheel, or what TestPyPI/PyPI serve).

    python -I scripts/smoke_test.py [--version X.Y.Z]

Run it with -I in an environment where the package is installed, not from the source tree."""

import argparse
import asyncio
from importlib import metadata, resources
from pathlib import PurePath

#: The commands the distribution installs (console scripts), next to the interpreter.
COMMANDS = {"fastapi-alcazaba"}


def check(condition: bool, message: str) -> None:
    """Not `assert`: this must also fail under `python -O`."""
    if not condition:
        raise SystemExit(f"smoke test failed: {message}")


def check_distribution(expected_version: str | None) -> None:
    import fastapi_alcazaba

    installed = metadata.version("fastapi-alcazaba")
    check(
        fastapi_alcazaba.__version__ == installed,
        f"__version__ {fastapi_alcazaba.__version__} != {installed}",
    )
    if expected_version is not None:
        check(installed == expected_version, f"installed {installed}, expected {expected_version}")
    files = [str(path) for path in metadata.files("fastapi-alcazaba") or []]
    entry_points = metadata.distribution("fastapi-alcazaba").entry_points
    commands = {point.name for point in entry_points if point.group == "console_scripts"}
    check(commands == COMMANDS, f"commands {sorted(commands)}, expected {sorted(COMMANDS)}")
    stray = [f for f in files if not f.startswith("fastapi_alcazaba") and not _command(f)]
    check(not stray, f"files outside the package: {stray}")
    check(any(f.endswith("licenses/LICENSE") for f in files), "LICENSE missing")
    check(any(f.endswith("licenses/NOTICE") for f in files), "NOTICE missing")
    check(resources.files("fastapi_alcazaba").joinpath("py.typed").is_file(), "py.typed missing")
    print(f"fastapi-alcazaba {installed}: {len(files)} files")


def _command(file: str) -> bool:
    """Whether `file` is one of COMMANDS, in the environment's bin/ (Scripts\\ on Windows)."""
    path = PurePath(file)
    return path.parent.name in ("bin", "Scripts") and path.stem in COMMANDS


async def check_cache() -> None:
    from fastapi_alcazaba import FastAPICache
    from fastapi_alcazaba.backends.inmemory import InMemoryBackend
    from fastapi_alcazaba.response_cache import cache

    calls: list[int] = []
    FastAPICache.init(InMemoryBackend(), signing_key="smoke-test-signing-key-0123456789abcdef")

    @cache(expire=60)
    async def value(x: int) -> dict[str, int]:
        calls.append(x)
        return {"x": x}

    check(await value(1) == {"x": 1}, "miss returned the wrong value")
    check(await value(1) == {"x": 1}, "hit returned the wrong value")
    check(calls == [1], f"computed {len(calls)} times instead of once")
    print("cache: miss then hit")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="the version the package must report")
    args = parser.parse_args()
    check_distribution(args.version)
    asyncio.run(check_cache())


if __name__ == "__main__":
    main()
