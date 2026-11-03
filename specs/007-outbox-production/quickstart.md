# Quickstart

```bash
.venv/bin/pytest tests/unit/test_outbox.py
make services-up   # postgres:17 on 15432 among them
POSTGRES_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:15432/postgres \
  .venv/bin/pytest -m integration tests/integration/test_outbox_live.py
```
