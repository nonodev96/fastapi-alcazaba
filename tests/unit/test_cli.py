"""The `fastapi-alcazaba` command, on queues of a module made for each test."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import json
import sys
import types
from collections.abc import Callable, Coroutine
from typing import Any

import fakeredis
import pytest

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.redis import RedisBackend
from fastapi_alcazaba.background import FastAPITaskQueue
from fastapi_alcazaba.cli import NOT_DONE, USAGE, main

SIGNING_KEY = "s" * 32


class App:
    """A module `worker` with two queues, as an app has them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.server = fakeredis.FakeServer()
        self.module = types.ModuleType("worker")
        self.emails = self.queue("emails")
        self.reports = self.queue("reports")

        @self.emails.task("send", retries=0)
        async def send(to: str) -> None:
            raise ConnectionError(to)

        @self.reports.task("noop")
        async def noop() -> None: ...

        async def make_queue() -> FastAPITaskQueue:
            return self.emails

        self.module.emails = self.emails  # type: ignore[attr-defined]
        self.module.reports = self.reports  # type: ignore[attr-defined]
        self.module.make_queue = make_queue  # type: ignore[attr-defined]
        self.module.sync_queue = lambda: self.reports  # type: ignore[attr-defined]
        self.module.settings = {"url": "redis://"}  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "worker", self.module)

    def queue(self, name: str) -> FastAPITaskQueue:
        backend = RedisBackend(fakeredis.FakeAsyncRedis(server=self.server))
        cache = Cache(backend, prefix="app", signing_key=SIGNING_KEY)
        return FastAPITaskQueue(cache, name=name, poll_interval=0.02)

    def run(self, work: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        return asyncio.run(work())

    def dead_letters(self, count: int) -> list[str]:
        """`count` tasks that failed in the emails queue: their task ids."""

        async def fail() -> list[str]:
            ids = [await self.emails.enqueue("send", f"user{n}@example.com") for n in range(count)]
            async with self.emails:
                while await self.emails.dead_letter_count() < count:
                    await asyncio.sleep(0.01)
            return ids

        ids: list[str] = self.run(fail)
        return ids


@pytest.fixture(name="app")
def fixture_app(monkeypatch: pytest.MonkeyPatch) -> App:
    return App(monkeypatch)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


class TestTasks:
    def test_queues(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        app.dead_letters(2)
        app.run(lambda: app.reports.enqueue("noop"))
        status, out, _ = run(capsys, "tasks", "worker", "queues")
        assert status == 0
        assert out.splitlines() == [
            "NAME     PENDING  DEAD_LETTERS",
            "emails   0        2",
            "reports  1        0",
        ]
        status, out, _ = run(capsys, "tasks", "worker", "--json", "queues")
        assert json.loads(out) == [
            {"name": "emails", "pending": 0, "dead_letters": 2},
            {"name": "reports", "pending": 1, "dead_letters": 0},
        ]

    def test_pending(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        app.run(lambda: app.emails.enqueue("send", "a@example.com"))
        assert run(capsys, "tasks", "worker:emails", "pending") == (0, "pending: 1\n", "")
        _, out, _ = run(capsys, "tasks", "worker:emails", "--json", "pending")
        assert json.loads(out) == {"pending": 1}

    def test_dead_letters(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        assert run(capsys, "tasks", "worker:emails", "dead-letters")[1] == "(none)\n"
        ids = app.dead_letters(3)
        status, out, _ = run(capsys, "tasks", "worker:emails", "dead-letters", "-n", "2")
        lines = out.splitlines()
        assert status == 0
        assert lines[0].split() == [
            "ID",
            "TASK",
            "TASK_ID",
            "REASON",
            "ERROR",
            "ATTEMPTS",
            "FAILED_AT",
        ]
        assert len(lines) == 3
        assert "send" in lines[1]
        assert "ConnectionError" in lines[1]
        assert "example.com" not in out  # no arguments, no error messages
        status, out, _ = run(capsys, "tasks", "worker:emails", "--json", "dead-letters")
        letters = json.loads(out)
        assert {letter["task_id"] for letter in letters} == set(ids)
        assert letters[0]["failed_at"].endswith("+00:00")

    def test_requeue_and_discard(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        app.dead_letters(3)
        letters = app.run(app.emails.dead_letters)
        status, out, _ = run(capsys, "tasks", "worker:emails", "requeue", letters[0].id)
        assert (status, out) == (0, "requeued: 1\n")
        status, out, _ = run(capsys, "tasks", "worker:emails", "discard", letters[1].id, "1-1")
        assert (status, out) == (0, "discarded: 1\n")
        assert run(capsys, "tasks", "worker:emails", "requeue", "--all")[1] == "requeued: 1\n"
        assert app.run(app.emails.dead_letter_count) == 0
        assert app.run(app.emails.pending) == 2

    @pytest.mark.parametrize("argv", [["requeue"], ["requeue", "--all", "1-0"]])
    def test_requeue_needs_ids_or_all(
        self,
        app: App,
        capsys: pytest.CaptureFixture[str],
        argv: list[str],
    ) -> None:
        status, _, err = run(capsys, "tasks", "worker:emails", *argv)
        assert status == USAGE
        assert "or --all" in err
        assert app.run(app.emails.dead_letter_count) == 0

    def test_status_and_cancel(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        task_id = app.run(lambda: app.emails.enqueue("send", "a@example.com"))
        status, out, _ = run(capsys, "tasks", "worker:emails", "status", task_id)
        assert status == 0
        assert f"id: {task_id}" in out
        assert "state: pending" in out
        assert "error: -" in out
        status, out, _ = run(capsys, "tasks", "worker:emails", "--json", "status", task_id)
        assert json.loads(out)["state"] == "pending"
        assert run(capsys, "tasks", "worker:make_queue", "cancel", task_id)[:2] == (
            0,
            "cancelled: True\n",
        )
        status, out, err = run(capsys, "tasks", "worker:make_queue", "cancel", task_id)
        assert (status, out) == (NOT_DONE, "cancelled: False\n")
        assert "not cancelled" in err
        status, _, err = run(capsys, "tasks", "worker:emails", "status", "a" * 32)
        assert status == NOT_DONE
        assert "no task" in err

    @pytest.mark.parametrize(
        ("target", "message"),
        [
            ("nowhere.at.all", "can't import nowhere.at.all"),
            ("worker:missing", "worker has no missing"),
            ("worker:settings", "isn't a FastAPITaskQueue"),
            ("worker:settings.url", "worker has no settings.url"),
            ("worker", "several queues (emails, reports)"),
            ("json", "json has no FastAPITaskQueue"),
        ],
    )
    def test_wrong_targets(
        self,
        app: App,
        capsys: pytest.CaptureFixture[str],
        target: str,
        message: str,
    ) -> None:
        del app
        status, _, err = run(capsys, "tasks", target, "pending")
        assert status == USAGE
        assert message in err

    def test_a_sync_factory(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        del app
        assert run(capsys, "tasks", "worker:sync_queue", "pending")[1] == "pending: 0\n"

    def test_a_malformed_id(self, app: App, capsys: pytest.CaptureFixture[str]) -> None:
        del app
        status, _, err = run(capsys, "tasks", "worker:emails", "status", "nope")
        assert status == USAGE
        assert "isn't the ID of a task" in err

    def test_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exited:
            main(["tasks"])
        assert exited.value.code == USAGE
        assert "usage: fastapi-alcazaba tasks" in capsys.readouterr().err
