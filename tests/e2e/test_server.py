"""End-to-end: tests/e2e/app.py served by uvicorn with two worker processes, over real HTTP,
against a real Valkey (VALKEY_URL). What the unit tests can only simulate: several processes
sharing the cache, its lock and its tags."""

import asyncio
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
import valkey

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


class Server:
    """The app under uvicorn; `like=` another server shares its prefix, channel and key, as
    another container of the same app would."""

    def __init__(self, valkey_url: str, workers: int = 2, *, like: "Server | None" = None) -> None:
        self.prefix: str = like.prefix if like else f"fastapi-alcazaba-e2e-{uuid.uuid4().hex}"
        self.encryption_key: str = like.encryption_key if like else secrets.token_urlsafe(32)
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = {
            **os.environ,
            "ALCAZABA_E2E_VALKEY_URL": valkey_url,
            "ALCAZABA_E2E_PREFIX": self.prefix,
            "ALCAZABA_E2E_ENCRYPTION_KEY": self.encryption_key,
        }
        self.process = subprocess.Popen(  # noqa: S603  # our own interpreter and arguments
            [
                sys.executable,
                "-m",
                "uvicorn",
                "tests.e2e.app:app",
                "--host=127.0.0.1",
                f"--port={self.port}",
                f"--workers={workers}",
                "--log-level=warning",
            ],
            cwd=ROOT,
            env=env,
        )

    def wait_ready(self, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"uvicorn exited with {self.process.returncode}")
            try:
                if httpx2.get(f"{self.base_url}/health", timeout=0.5).status_code == 200:
                    return
            except httpx2.TransportError:
                pass
            time.sleep(0.1)
        raise TimeoutError("uvicorn didn't start")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture(scope="module")
def valkey_url() -> str:
    url = os.environ.get("VALKEY_URL")
    if not url:
        pytest.skip("VALKEY_URL not set")
    return url


@pytest.fixture(scope="module")
def server(valkey_url: str) -> Iterator[Server]:
    running = Server(valkey_url)
    try:
        running.wait_ready()
        yield running
    finally:
        running.stop()
        client = valkey.from_url(valkey_url)  # type: ignore[no-untyped-call]
        keys = list(client.scan_iter(match=f"{running.prefix}:*"))
        if keys:
            client.delete(*keys)


def computed(valkey_url: str, server: Server, name: str) -> int:
    client = valkey.from_url(valkey_url)  # type: ignore[no-untyped-call]
    return int(client.get(f"{server.prefix}:computed:{name}") or 0)


async def test_processes_share_the_value_and_compute_it_once(
    valkey_url: str, server: Server
) -> None:
    """20 concurrent requests spread over two processes: the distributed lock makes one of them
    compute the value while the others wait for it."""
    async with httpx2.AsyncClient(base_url=server.base_url, timeout=10) as client:
        responses = await asyncio.gather(*(client.get("/slow", params={"x": 1}) for _ in range(20)))
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["pid"] for response in responses}) == 1  # one computation
    assert computed(valkey_url, server, "slow:1") == 1
    assert {response.headers["X-FastAPI-Cache"] for response in responses} <= {"HIT", "MISS"}


async def test_users_get_their_own_responses(valkey_url: str, server: Server) -> None:
    async with httpx2.AsyncClient(base_url=server.base_url) as client:
        alice = await client.get("/me", headers={"Authorization": "Bearer alice"})
        bob = await client.get("/me", headers={"Authorization": "Bearer bob"})
        again = await client.get("/me", headers={"Authorization": "Bearer alice"})
    assert alice.json() == {"user": "Bearer alice"}
    assert bob.json() == {"user": "Bearer bob"}
    assert again.headers["X-FastAPI-Cache"] == "HIT"
    assert again.headers["Cache-Control"].startswith("private, max-age=")
    assert computed(valkey_url, server, "me") == 2


async def test_conditional_requests(server: Server) -> None:
    async with httpx2.AsyncClient(base_url=server.base_url) as client:
        first = await client.get("/slow", params={"x": 2})
        etag = first.headers["ETag"]
        unchanged = await client.get("/slow", params={"x": 2}, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""


async def test_invalidating_a_tag_reaches_every_process(valkey_url: str, server: Server) -> None:
    """One process computes the value; another reads it from Valkey and keeps a copy in memory,
    without its tags. After the first invalidates the tag, neither serves the value again."""
    first, second = (Server(valkey_url, workers=1, like=server) for _ in range(2))
    try:
        first.wait_ready()
        second.wait_ready()
        async with (
            httpx2.AsyncClient(base_url=first.base_url) as one,
            httpx2.AsyncClient(base_url=second.base_url) as other,
        ):

            async def computation(client: httpx2.AsyncClient) -> int:
                computed: int = (await client.get("/products/7")).json()["computation"]
                return computed

            before = [await computation(one), await computation(other)]
            await asyncio.sleep(0.3)  # the second subscribed to the channel on its first request
            before.append(await computation(other))  # now kept in its memory
            invalidated = await one.post("/products/7/invalidate")
            await asyncio.sleep(0.5)  # the announcement takes milliseconds
            after = [await computation(other), await computation(one)]
    finally:
        first.stop()
        second.stop()
    assert invalidated.json() == {"deleted": 1}
    assert (before, after) == ([1, 1, 1], [2, 2])
    assert computed(valkey_url, server, "product:7") == 2


async def test_processes_share_the_rate_limit(server: Server) -> None:
    """The counters live in Valkey, not in each process (nor in TieredBackend's local copies)."""
    async with httpx2.AsyncClient(base_url=server.base_url, timeout=10) as client:
        responses = await asyncio.gather(*(client.get(f"/limited/{i}") for i in range(20)))
    codes = [response.status_code for response in responses]
    assert codes.count(200) == 5
    assert codes.count(429) == 15
    assert all(int(r.headers["Retry-After"]) > 0 for r in responses if r.status_code == 429)
    assert all(r.headers["RateLimit-Policy"] == '"5/60s";q=5;w=60' for r in responses)
    allowed = [r.headers["RateLimit"] for r in responses if r.status_code == 200]
    assert sorted(re.findall(r";r=(\d+)", ",".join(allowed))) == ["0", "1", "2", "3", "4"]


async def test_an_unreachable_valkey_does_not_break_the_app() -> None:
    """Fail open: nothing listens on that port; requests run uncached and don't hang."""
    down = Server(f"valkey://127.0.0.1:{free_port()}/0", workers=1)
    try:
        down.wait_ready()
        async with httpx2.AsyncClient(base_url=down.base_url, timeout=10) as client:
            start = time.monotonic()
            responses = [await client.get("/slow", params={"x": 3}) for _ in range(5)]
            elapsed = time.monotonic() - start
    finally:
        down.stop()
    assert {response.status_code for response in responses} == {200}
    assert {response.headers.get("X-FastAPI-Cache") for response in responses} <= {None, "MISS"}
    assert elapsed < 5  # 0.3 s of work each, no waiting for the backend once the breaker opens
