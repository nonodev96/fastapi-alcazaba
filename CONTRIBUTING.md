# Contributing to Alcazaba

Thanks for helping! Bug reports, fixes, docs and new backends are all welcome. For security
problems, don't open an issue: follow [SECURITY.md](SECURITY.md).

## Setting up

You need Python 3.11 or later, `make` and, for the integration tests, Docker. `make deps` makes
a virtual environment in `.venv` with Python's own `venv` and pip (26.2 or later, which it
installs there), with every extra and the development tools.

```shell
git clone https://github.com/nonodev96/fastapi-alcazaba && cd fastapi-alcazaba
make deps
.venv/bin/pre-commit install --install-hooks -t pre-commit -t pre-push
```

## Tests

| Command | What runs |
| --- | --- |
| `.venv/bin/pytest` | unit tests and typing checks (no servers; integration and e2e tests skip) |
| `make services-up` | throwaway Redis, Valkey, a Valkey cluster, memcached and DynamoDB Local |
| `make test-integration` | every backend against those servers |
| `VALKEY_URL=valkey://127.0.0.1:16380/15 .venv/bin/pytest tests/e2e` | the example app in uvicorn processes |
| `.venv/bin/pytest tests/benchmarks --benchmark-only` | benchmarks |
| `make mutation` | mutation testing (slow) |
| `make services-down` | stops the servers |

CI runs all of them on Python 3.11 to 3.14; a pull request needs them green, with coverage of at
least 90 %.

### Fuzzing

`fuzz/` holds fuzz targets for what reads data from outside (uploads, cursors, webhooks, stored
entries, audit details), each with the properties it must keep. `tests/unit/test_fuzz_targets.py`
runs them on random inputs with the other unit tests; ClusterFuzzLite runs them with atheris,
guided by coverage, on pull requests that touch the package and every week
(`.github/workflows/fuzzing.yml`). An input that breaks a target goes in that test's known cases
along with the fix. Locally, on Linux, `pip install atheris` and `python fuzz/uploads_fuzzer.py`
fuzz one until you stop it. The fuzzing build installs the package's requirements pinned by hash,
from `.clusterfuzzlite/requirements.txt`; when they change, `make fuzz-requirements` (with Python
3.11, the one in OSS-Fuzz's image) writes it again.

## Guidelines

- Keep the secure defaults: anything that could serve one user's data to another, trust data
  read from the backend, or let a client bypass the cache must be opt-in and documented.
- Every change comes with tests; a bug fix with a test that fails without it.
- `ruff`, `ruff format`, `mypy --strict` and `pylint` must pass (pre-commit runs them).
- A signature that doesn't fit on one line has one parameter per line, the last one with a
  comma. `make format` (and pre-commit) adds the comma and lets `ruff format` do the rest;
  `make lint` checks it with `scripts/check_signatures.py`.
- Write commit messages in English: a short summary line, then what changed and why.
- Note user-visible changes in `CHANGELOG.md` under "Unreleased".
- The guide of the documentation is the README's: each page of `docs/guide/` includes sections
  of `README.md` by name. Every `## ` section of the README has its body between
  `<!-- --8<-- [start:anchor] -->` and `<!-- --8<-- [end:anchor] -->` (its GitHub anchor), which
  GitHub and PyPI don't show; a new section needs those markers and a place in a guide page.
  `make docs` fails on a section or a link that isn't there.
- New dependencies: only when they're worth it, as optional extras where possible, and never a
  release younger than three days (pip enforces it: `make deps` and CI set
  `PIP_UPLOADED_PRIOR_TO=P3D`).

## Releases

Maintainers release by pushing a `vX.Y.Z` tag; see [docs/releasing.md](docs/releasing.md).
