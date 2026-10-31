# Development

```shell
make deps                     # .venv (Python's venv and pip), with every extra and the dev tools
.venv/bin/pytest              # unit tests
make services-up              # Redis, Valkey, a Valkey cluster, memcached and DynamoDB Local
make test-integration         # every backend against those servers
make services-down
make mutation                 # mutation testing (slow; CI runs it weekly)
make lint                     # ruff, ruff format --check, mypy --strict, pylint
make docs                     # the documentation, built as CI does (make docs-serve to preview it)
```

Tests run in a random order (pytest-randomly; the seed is printed to reproduce a failure), and CI
fails a pull request with a benchmark more than 50 % slower than main's, compared with the others
so the speed of the runner doesn't count. See
[CONTRIBUTING.md](https://github.com/nonodev96/fastapi-alcazaba/blob/main/CONTRIBUTING.md).
