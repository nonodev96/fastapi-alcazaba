"""Server sessions as the backend holds them: whatever bytes are under a session's key or a user's
index (planted, corrupted, of another version), reading them gives a live session or none, and an
index or an empty one; nothing is raised."""

import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba import Cache
    from fastapi_alcazaba.backends.inmemory import InMemoryBackend
    from fastapi_alcazaba.security import ServerSessionMiddleware, UserSessions

_CACHE = Cache(InMemoryBackend())  # unsigned: what's read is what was stored
_USERS = UserSessions()
_MIDDLEWARE = ServerSessionMiddleware(lambda *_: None, users=_USERS, max_age=3600)  # type: ignore[arg-type]
_NOW = 1_800_000_000


def fuzz_one_input(data: bytes) -> None:
    record = _MIDDLEWARE._open(_CACHE, "k", data, _NOW)  # pylint: disable=protected-access
    if record is not None:
        session, created, seen, started = record
        assert isinstance(session, dict)
        assert all(type(value) is int for value in (created, seen, started))
        assert _NOW - created < 3600
    found = _USERS._record(_CACHE, "k", data, "ana")  # pylint: disable=protected-access
    assert found is None or isinstance(found.get("data"), dict)


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
