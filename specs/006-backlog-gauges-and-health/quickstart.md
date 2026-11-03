# Quickstart

```bash
.venv/bin/pytest tests/unit/test_gauges.py tests/unit/test_health_checks.py
```

By hand: an app with a queue, a `BacklogMonitor(queue, interval=1)` and the OpenTelemetry SDK's
console exporter: enqueue delayed tasks and watch `fastapi_alcazaba.tasks.backlog`.
