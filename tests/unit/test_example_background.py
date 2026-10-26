"""The background example: a task queue, scheduled jobs and a leader, on fakeredis."""

import asyncio
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import fakeredis
import pytest
from fastapi.testclient import TestClient

from examples.background import main
from fastapi_alcazaba import FastAPICache

ERROR = "examples.background.main.MailerUnavailable"


@pytest.fixture(name="server", scope="module")
def fixture_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture(name="client", scope="module")
def fixture_client(server: fakeredis.FakeServer) -> Iterator[TestClient]:
    # One app for the whole module: every `with TestClient(app)` runs in an event loop of its own,
    # and a FastAPITaskQueue can't be started again in another loop.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(main, "redis_client", lambda: fakeredis.FakeAsyncRedis(server=server))
        patch.setattr(main.queue, "poll_interval", 0.01)
        patch.setattr(main, "RELAY_INTERVAL", 0.01)
        with TestClient(main.app) as client:
            yield client
    FastAPICache.reset()


@pytest.fixture(autouse=True)
def stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database, a provider and a broker of its own for each test."""
    monkeypatch.setattr(main, "db", main.Database())
    monkeypatch.setattr(main, "mailer", main.Mailer())
    monkeypatch.setattr(main, "broker", main.Broker())


ADMIN = {"Authorization": f"Bearer {main.ADMIN_TOKEN}"}


def eventually(check: Callable[[], Any], timeout: float = 3.0) -> Any:
    """What `check()` returns once it's true."""
    deadline = time.monotonic() + timeout
    while not (result := check()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)
    return result


def delayed(server: fakeredis.FakeServer) -> list[bytes]:
    """The retries waiting for their time in Redis."""
    with fakeredis.FakeRedis(server=server) as redis:
        return [
            task
            for key in redis.scan_iter("signup!tasks:*:delayed")
            for task in redis.zrange(key, 0, -1)
        ]


def fast_forward(server: fakeredis.FakeServer) -> None:
    """Make the retries due now, instead of in 10 s, 20 s, 40 s..."""
    with fakeredis.FakeRedis(server=server) as redis:
        for key in redis.scan_iter("signup!tasks:*:delayed"):
            redis.zadd(key, dict.fromkeys(redis.zrange(key, 0, -1), 0))


def sign_up(client: TestClient) -> int:
    response = client.post("/users", json={"email": "ana@example.com", "name": "Ana"})
    assert response.status_code == 201
    user_id: int = response.json()["id"]
    return user_id


def settled(client: TestClient) -> bool:
    """No task left to run or retry: nothing carries over to the next test."""
    pending: int = client.get("/admin/background", headers=ADMIN).json()["pending_tasks"]
    return pending == 0


class TestWelcomeEmails:
    def test_the_queue_sends_it_after_the_response(self, client: TestClient) -> None:
        user_id = sign_up(client)
        eventually(lambda: client.get(f"/users/{user_id}").json()["welcomed"])
        assert main.mailer.sent == [("ana@example.com", "Welcome, Ana!")]
        eventually(lambda: settled(client))

    def test_a_second_run_sends_nothing(self) -> None:
        user = main.db.add_user("ana@example.com", "Ana")
        asyncio.run(main.send_welcome_email(user.id))
        asyncio.run(main.send_welcome_email(user.id))  # at least once: it may run twice
        asyncio.run(main.send_welcome_email(404))  # a user deleted meanwhile
        assert main.mailer.sent == [("ana@example.com", "Welcome, Ana!")]

    def test_invalid_sign_ups(self, client: TestClient) -> None:
        response = client.post("/users", json={"email": "not an address", "name": "Ana"})
        assert response.status_code == 422
        assert client.get("/users/1").status_code == 404


class TestRetriesAndDeadLetters:
    def test_retried_while_the_provider_is_down(
        self,
        client: TestClient,
        server: fakeredis.FakeServer,
    ) -> None:
        main.mailer.available = False
        user_id = sign_up(client)
        [task] = eventually(lambda: delayed(server))  # its retry, waiting for its time
        assert b'"args":[%d]' % user_id in task
        assert b"ana@example.com" not in task  # only the ID goes through Redis
        main.mailer.available = True
        fast_forward(server)
        eventually(lambda: client.get(f"/users/{user_id}").json()["welcomed"])
        eventually(lambda: settled(client))

    def test_out_of_retries_a_dead_letter_that_admins_requeue(
        self,
        client: TestClient,
        server: fakeredis.FakeServer,
    ) -> None:
        down = client.put("/admin/simulate/mailer?down=true", headers=ADMIN)
        assert down.status_code == 204
        user_id = sign_up(client)

        def dead() -> list[dict[str, Any]]:
            fast_forward(server)
            letters: list[dict[str, Any]] = client.get("/admin/dead-letters", headers=ADMIN).json()
            return letters

        [letter] = eventually(dead)
        assert {key: letter[key] for key in ("reason", "task", "error", "attempts")} == {
            "reason": "failed",
            "task": "send-welcome-email",
            "error": ERROR,
            "attempts": 6,
        }
        assert set(letter) == {"id", "reason", "task", "task_id", "error", "attempts", "failed_at"}
        assert main.mailer.sent == []

        client.put("/admin/simulate/mailer?down=false", headers=ADMIN)  # the provider is back
        response = client.post("/admin/dead-letters/requeue", headers=ADMIN)
        assert response.json() == {"requeued": 1}
        eventually(lambda: client.get(f"/users/{user_id}").json()["welcomed"])
        assert client.get("/admin/dead-letters", headers=ADMIN).json() == []
        eventually(lambda: settled(client))


class TestAdmin:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/admin/background"),
            ("GET", "/admin/dead-letters"),
            ("POST", "/admin/dead-letters/requeue"),
            ("PUT", "/admin/simulate/mailer?down=true"),
        ],
    )
    def test_only_with_the_token(self, client: TestClient, method: str, path: str) -> None:
        assert client.request(method, path).status_code == 401
        wrong = {"Authorization": "Bearer guessed"}
        assert client.request(method, path, headers=wrong).status_code == 403

    def test_what_the_process_is_doing(self, client: TestClient) -> None:
        body = client.get("/admin/background", headers=ADMIN).json()
        assert (body["leader"], body["pending_tasks"]) == (True, 0)
        hourly = datetime.fromisoformat(body["next_runs"]["end-trials"])
        assert (hourly.minute, hourly.second, hourly.tzinfo) == (0, 0, UTC)
        assert timedelta(0) < hourly - datetime.now(UTC) <= timedelta(hours=1)
        report = datetime.fromisoformat(body["next_runs"]["signup-report"])
        in_madrid = report.astimezone(ZoneInfo("Europe/Madrid"))
        assert (in_madrid.hour, in_madrid.minute, in_madrid.isoweekday() <= 5) == (8, 0, True)


class TestScheduledJobs:
    def test_trials_end(self) -> None:
        old = main.db.add_user("old@example.com", "Old", datetime.now(UTC) - timedelta(days=15))
        new = main.db.add_user("new@example.com", "New")
        asyncio.run(main.end_trials())
        assert (old.plan, new.plan) == ("free", "trial")

    def test_the_signup_report(self) -> None:
        main.db.add_user("old@example.com", "Old", datetime.now(UTC) - timedelta(days=2))
        main.db.add_user("new@example.com", "New")
        asyncio.run(main.signup_report())
        assert main.mailer.sent == [("team@example.com", "1 sign-ups in the last 24 hours")]


class TestOutboxRelay:
    def test_the_leader_publishes_the_events(self, client: TestClient) -> None:
        user_id = sign_up(client)
        eventually(lambda: main.broker.published)
        assert main.broker.published == [{"type": "user.created", "user_id": user_id}]
        assert main.db.outbox == []
        eventually(lambda: settled(client))
