"""The ready-made health checks of Alcazaba's components, alone and in the health router."""

# pylint: disable=protected-access  # tests look at the internals they check

import asyncio
import http.server
import socket
import ssl
import threading
from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.background import FastAPIBroadcast, FastAPILeader
from fastapi_alcazaba.mail.sending import MailNotSent, SMTPSender
from fastapi_alcazaba.observability import (
    HealthCheck,
    broadcast_check,
    health_router,
    leader_check,
    object_storage_check,
    smtp_check,
    task_queue_check,
)
from fastapi_alcazaba.response_cache.caches import Cache
from fastapi_alcazaba.uploads.s3 import S3Presigner
from tests.unit.test_mail import PASSWORD, fixture_tls, serve
from tests.unit.test_tasks import World, eventually

__all__ = ["fixture_tls"]  # the `tls` fixture, from the mail tests


async def passes(check: HealthCheck) -> bool:
    """Whether the check passes, as the router decides it."""
    try:
        result = check.check()
        if asyncio.iscoroutine(result):
            result = await result
    except Exception:
        return False
    return result is not False


async def passes_in_thread(check: HealthCheck) -> bool:
    """A sync check, run off the loop as the router runs it."""
    try:
        result = await asyncio.to_thread(check.check)
    except Exception:
        return False
    return result is not False


class TestTaskQueue:
    async def test_thresholds(self) -> None:
        world = World()
        queue = world.queue(name="emails")
        queue.task("job")(_noop)
        assert await passes(task_queue_check(queue))
        for _ in range(3):
            await queue.enqueue("job", delay=60)
        assert await passes(task_queue_check(queue, max_backlog=3))
        assert not await passes(task_queue_check(queue, max_backlog=2))
        await world.plant(queue, {"p": b"not a task"})
        async with queue:
            await eventually(queue.dead_letters)
        assert await passes(task_queue_check(queue, max_dead_letters=1))
        assert not await passes(task_queue_check(queue, max_dead_letters=0))

    async def test_a_backend_that_doesnt_answer(self) -> None:
        world = World()
        queue = world.queue()
        world.server.connected = False  # fakeredis: every command fails to connect
        assert not await passes(task_queue_check(queue))

    def test_defaults_and_options(self) -> None:
        queue = World().queue()
        check = task_queue_check(queue)
        assert (check.critical, check.timeout) == (False, None)
        check = task_queue_check(queue, critical=True, timeout=3)
        assert (check.critical, check.timeout) == (True, 3)
        with pytest.raises(ValueError, match="can't be negative"):
            task_queue_check(queue, max_dead_letters=-1)


async def _noop() -> None:
    pass


def connected(broadcast: FastAPIBroadcast) -> bool | None:
    return broadcast.connected  # read anew each time, not narrowed by the asserts before


def campaigning(leader: FastAPILeader) -> bool:
    return leader.campaigning


class TestBroadcast:
    async def test_connected_and_reconnecting(self) -> None:
        broadcast = FastAPIBroadcast(fakeredis.FakeAsyncRedis())
        assert connected(broadcast) is None  # nobody subscribed here: not listening
        assert await passes(broadcast_check(broadcast))
        async with broadcast.subscribe("room"):
            await eventually(lambda: connected(broadcast))
            assert await passes(broadcast_check(broadcast))
            pubsub, broadcast._pubsub = broadcast._pubsub, None  # as while it reconnects
            assert connected(broadcast) is False
            assert not await passes(broadcast_check(broadcast))
            broadcast._pubsub = pubsub
        await broadcast.close()
        assert connected(broadcast) is None

    async def test_redis_that_doesnt_answer(self) -> None:
        class Down(fakeredis.FakeAsyncRedis):  # pylint: disable=abstract-method
            async def ping(self, **kwargs: Any) -> Any:  # pylint: disable=invalid-overridden-method
                raise ConnectionError("down")

        assert not await passes(broadcast_check(FastAPIBroadcast(Down())))

    async def test_in_this_process_only(self) -> None:
        broadcast = FastAPIBroadcast()
        assert connected(broadcast) is None
        assert await passes(broadcast_check(broadcast))


class TestLeader:
    async def test_campaigning(self) -> None:
        leader = FastAPILeader("relay", ttl=0.3, cache=Cache(InMemoryBackend()))
        check = leader_check(leader)
        assert not campaigning(leader)
        assert not await passes(check)
        await leader.start()
        assert campaigning(leader)
        assert await passes(check)
        assert leader._campaign is not None
        leader._campaign.cancel()  # its campaign died
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not await passes(check)
        await leader.stop()
        assert not await passes(check)


class TestSMTP:
    async def test_a_server_that_answers(self, tls: tuple[ssl.SSLContext, ssl.SSLContext]) -> None:
        with serve(tls[0]) as server:
            port = server.server_address[1]
            sender = SMTPSender(
                "localhost", port, username="user", password=PASSWORD, ssl_context=tls[1]
            )
            assert await passes(smtp_check(sender))
            assert server.received == []  # nothing sent
            wrong = SMTPSender(
                "localhost", port, username="user", password=PASSWORD + "-wrong", ssl_context=tls[1]
            )
            assert not await passes(smtp_check(wrong))
            with pytest.raises(MailNotSent, match="SMTPAuthenticationError"):
                await wrong.check()

    async def test_nobody_listening(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        sender = SMTPSender("localhost", port, timeout=1)
        assert not await passes(smtp_check(sender))

    async def test_a_server_that_refuses_noop(
        self,
        tls: tuple[ssl.SSLContext, ssl.SSLContext],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import smtplib

        monkeypatch.setattr(smtplib.SMTP, "noop", lambda self: (421, b"closing"))
        with serve(tls[0]) as server:
            sender = SMTPSender(
                "localhost",
                server.server_address[1],
                username="user",
                password=PASSWORD,
                ssl_context=tls[1],
            )
            with pytest.raises(MailNotSent, match="SMTPResponseException"):
                await sender.check()

    async def test_tls_refused_closes_the_connection(
        self,
        tls: tuple[ssl.SSLContext, ssl.SSLContext],
    ) -> None:
        with serve(tls[0]) as server:
            sender = SMTPSender("localhost", server.server_address[1], timeout=2)
            with pytest.raises(MailNotSent, match="SSL"):
                # The system's authorities don't vouch for the test's certificate.
                await sender.check()


class Bucket(http.server.BaseHTTPRequestHandler):
    """Answers GETs of /probe/<key>: 206 for health/probe, 403 or 500 for the others."""

    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802  # pylint: disable=invalid-name  # http.server's name
        Bucket.seen.append({"path": self.path, "range": self.headers.get("Range", "")})
        if self.path.startswith("/probe/health/probe?"):
            self.send_response(206)
            self.send_header("Content-Length", "1")
            self.end_headers()
            self.wfile.write(b"x")
        elif self.path.startswith("/probe/broken?"):
            self.send_error(500)
        else:
            self.send_error(403)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture(name="bucket")
def fixture_bucket() -> Iterator[S3Presigner]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    Bucket.seen = []
    try:
        yield S3Presigner(
            bucket="probe",
            region="us-east-1",
            access_key="AKIDEXAMPLE",
            secret_key="secret-key-of-the-test",  # noqa: S106  # the tests' own
            endpoint=f"http://127.0.0.1:{server.server_address[1]}",
        )
    finally:
        server.shutdown()
        server.server_close()


class TestObjectStorage:
    async def test_reads_the_first_byte(self, bucket: S3Presigner) -> None:
        assert await passes_in_thread(object_storage_check(bucket, "health/probe"))
        assert len(Bucket.seen) == 1
        request = Bucket.seen[0]
        assert request["range"] == "bytes=0-0"
        assert "X-Amz-Signature=" in request["path"]

    @pytest.mark.parametrize("key", ["missing", "broken"])
    async def test_refused_or_broken(self, bucket: S3Presigner, key: str) -> None:
        assert not await passes_in_thread(object_storage_check(bucket, key, timeout=2))

    def test_needs_a_key(self, bucket: S3Presigner) -> None:
        with pytest.raises(ValueError, match="give the key"):
            object_storage_check(bucket, "")


class TestInTheRouter:
    def test_only_ok_or_fail(self, bucket: S3Presigner, caplog: pytest.LogCaptureFixture) -> None:
        leader = FastAPILeader("relay", cache=Cache(InMemoryBackend()))
        app = FastAPI()
        app.include_router(
            health_router(
                {
                    "leader": leader_check(leader),
                    "storage": object_storage_check(bucket, "missing"),
                    "critical_storage": object_storage_check(bucket, "broken", critical=True),
                },
                include_cache=False,
            )
        )
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert {name: check["status"] for name, check in body["checks"].items()} == {
            "leader": "fail",
            "storage": "fail",
            "critical_storage": "fail",
        }
        assert "127.0.0.1" not in response.text
        assert "AKIDEXAMPLE" not in response.text
        assert "Health check 'storage' failed" in caplog.text
