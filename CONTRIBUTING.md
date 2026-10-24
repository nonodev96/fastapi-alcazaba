# Contributing to Alcazaba

Thanks for helping! Bug reports, fixes, docs and new backends are all welcome. For security
problems, don't open an issue: follow [SECURITY.md](SECURITY.md).

## Setting up

You need [uv](https://docs.astral.sh/uv/) and, for the integration tests, Docker.

```shell
git clone https://github.com/nonodev96/fastapi-alcazaba && cd fastapi-alcazaba
uv sync --all-extras
uvx pre-commit install --install-hooks -t pre-commit -t pre-push
```

## Tests

| Command | What runs |
| --- | --- |
| `uv run pytest` | unit tests and typing checks (no servers; integration and e2e tests skip) |
| `make services-up` | throwaway Redis, Valkey, a Valkey cluster, memcached and DynamoDB Local |
| `make test-integration` | every backend against those servers |
| `VALKEY_URL=valkey://127.0.0.1:16380/15 uv run pytest tests/e2e` | the example app in uvicorn processes |
| `uv run pytest tests/benchmarks --benchmark-only` | benchmarks |
| `make mutation` | mutation testing (slow) |
| `make services-down` | stops the servers |

CI runs all of them on Python 3.11 to 3.14; a pull request needs them green, with coverage of at
least 90 %.

## Guidelines

- Keep the secure defaults: anything that could serve one user's data to another, trust data
  read from the backend, or let a client bypass the cache must be opt-in and documented.
- Every change comes with tests; a bug fix with a test that fails without it.
- `ruff`, `ruff format` and `mypy --strict` must pass (pre-commit runs them).
- Write commit messages in English: a short summary line, then what changed and why.
- Note user-visible changes in `CHANGELOG.md` under "Unreleased".
- New dependencies: only when they're worth it, as optional extras where possible, and never a
  release younger than three days (uv enforces it with `exclude-newer`).

## Releases

Maintainers release by pushing a `vX.Y.Z` tag; see [docs/releasing.md](docs/releasing.md).
