"""FastAPITOTP: RFC 6238's codes (its test vectors), sealed secrets, codes used once, lockouts,
recovery codes and step-up."""

import base64
import json
from typing import Annotated, Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi import Depends, FastAPI, Header, Request
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pydantic import SecretStr

from fastapi_alcazaba import Cache, FastAPIAudit, FastAPICache
from fastapi_alcazaba.audit import MemoryAuditSink
from fastapi_alcazaba.auth import FastAPITOTP, stepup
from fastapi_alcazaba.auth import totp as totp_module
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.backends.memcached import MemcachedBackend
from fastapi_alcazaba.security import ServerSessionMiddleware
from tests.unit.test_backends import FakeMemcache
from tests.unit.test_metrics import points

SECRET = "totp-secret-of-at-least-32-bytes!!"  # noqa: S105  # the tests' own
OTHER_SECRET = "another-secret-of-at-least-32-bytes"  # noqa: S105
NOW = 1_800_000_000.0  # a step boundary is every 30 s


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def time(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(totp_module, "time", clock)
    monkeypatch.setattr(stepup, "time", clock)
    return clock


def make(**options: Any) -> FastAPITOTP:
    return FastAPITOTP(
        **{"issuer": "Example", "secret": SECRET, "cache": Cache(InMemoryBackend()), **options}
    )


# RFC 6238, appendix B: the seeds (ASCII "1234567890" repeated) and the 8-digit codes.
SEEDS = {
    "SHA1": b"12345678901234567890",
    "SHA256": b"12345678901234567890123456789012",
    "SHA512": b"1234567890" * 6 + b"1234",
}
VECTORS = [
    (59, {"SHA1": "94287082", "SHA256": "46119246", "SHA512": "90693936"}),
    (1111111109, {"SHA1": "07081804", "SHA256": "68084774", "SHA512": "25091201"}),
    (1111111111, {"SHA1": "14050471", "SHA256": "67062674", "SHA512": "99943326"}),
    (1234567890, {"SHA1": "89005924", "SHA256": "91819424", "SHA512": "93441116"}),
    (2000000000, {"SHA1": "69279037", "SHA256": "90698825", "SHA512": "38618901"}),
    (20000000000, {"SHA1": "65353130", "SHA256": "77737706", "SHA512": "47863826"}),
]


@pytest.mark.parametrize(("at", "codes"), VECTORS)
@pytest.mark.parametrize("algorithm", ["SHA1", "SHA256", "SHA512"])
def test_rfc_6238_test_vectors(at: int, codes: dict[str, str], algorithm: Any) -> None:
    assert totp_module._counter_code(SEEDS[algorithm], at // 30, 8, algorithm) == codes[algorithm]  # pylint: disable=protected-access


class TestEnrollment:
    def test_the_uri_for_the_app(self) -> None:
        enrollment = make().enroll("u1", "ana@example.com")
        uri = urlsplit(enrollment.uri)
        label = unquote(uri.path)
        assert (uri.scheme, uri.netloc, label) == ("otpauth", "totp", "/Example:ana@example.com")
        query = {key: values[0] for key, values in parse_qs(uri.query).items()}
        assert query == {
            "secret": enrollment.secret.get_secret_value(),
            "issuer": "Example",
            "algorithm": "SHA1",
            "digits": "6",
            "period": "30",
        }
        raw = base64.b32decode(enrollment.secret.get_secret_value())
        assert len(raw) == 20
        assert isinstance(enrollment.secret, SecretStr)

    def test_the_sealed_secret_holds_nothing_in_clear(self) -> None:
        enrollment = make().enroll("u1", "ana")
        raw = base64.b32decode(enrollment.secret.get_secret_value())
        assert enrollment.sealed.startswith("totp1.")
        assert raw not in base64.urlsafe_b64decode(enrollment.sealed[6:])
        assert enrollment.secret.get_secret_value() not in enrollment.sealed

    @pytest.mark.usefixtures("clock")
    def test_it_only_opens_for_its_user(self) -> None:
        totp = make()
        sealed = totp.enroll("u1", "ana").sealed
        assert len(totp.code("u1", sealed)) == 6
        with pytest.raises(ValueError, match="isn't this user's"):
            totp.code("u2", sealed)
        with pytest.raises(ValueError, match="isn't this user's"):
            make(secret=OTHER_SECRET).code("u1", sealed)
        for tampered in ("", "totp1.!!!", "totp1." + sealed[6:-4] + "AAAA", sealed[6:]):
            with pytest.raises(ValueError, match="isn't this user's"):
                totp.code("u1", tampered)

    def test_the_secret_rotates(self) -> None:
        old = make(secret=SECRET)
        sealed = old.enroll("u1", "ana").sealed
        new = make(secret=["a-new-secret-of-at-least-32-bytes!!", SECRET])
        assert new.code("u1", sealed, at=NOW) == old.code("u1", sealed, at=NOW)

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"issuer": "Ex:ample"}, "issuer"),
            ({"digits": 7}, "digits"),
            ({"period": 5}, "period"),
            ({"window": 4}, "window"),
            ({"algorithm": "MD5"}, "algorithm"),
            ({"max_attempts": 0}, "max_attempts"),
        ],
    )
    def test_configuration(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            make(**options)

    def test_labels_and_ids(self) -> None:
        with pytest.raises(ValueError, match="label"):
            make().enroll("u1", "a:b")
        with pytest.raises(TypeError, match="user_id is an ID"):
            make().enroll(True, "ana")


class TestVerify:
    @pytest.mark.usefixtures("clock")
    async def test_the_current_code_and_the_window(self) -> None:
        totp = make()
        sealed = totp.enroll("u1", "ana").sealed
        for offset in (-30, 0, 30):
            assert await totp.verify("u1", sealed, totp.code("u1", sealed, at=NOW + offset))
        assert not await totp.verify("u1", sealed, totp.code("u1", sealed, at=NOW - 60))
        assert not await totp.verify("u1", sealed, totp.code("u1", sealed, at=NOW + 60))

    async def test_each_code_once(self, clock: Clock) -> None:
        totp = make()
        sealed = totp.enroll("u1", "ana").sealed
        code = totp.code("u1", sealed)
        assert await totp.verify("u1", sealed, code)
        assert not await totp.verify("u1", sealed, code)  # someone who saw it
        clock.now += 30
        assert await totp.verify("u1", sealed, totp.code("u1", sealed))

    @pytest.mark.usefixtures("clock")
    async def test_codes_with_spaces(self) -> None:
        totp = make()
        sealed = totp.enroll("u1", "ana").sealed
        code = totp.code("u1", sealed)
        assert await totp.verify("u1", sealed, f"{code[:3]} {code[3:]}")

    @pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "１２３４５６"])
    @pytest.mark.usefixtures("clock")
    async def test_malformed_codes(self, code: str) -> None:
        totp = make()
        assert not await totp.verify("u1", totp.enroll("u1", "ana").sealed, code)

    @pytest.mark.usefixtures("clock")
    async def test_a_lockout_after_too_many_wrong_codes(self) -> None:
        totp = make(max_attempts=3)
        sealed = totp.enroll("u1", "ana").sealed
        wrong = f"{(int(totp.code('u1', sealed)) + 1) % 10**6:06d}"
        for _ in range(3):
            assert not await totp.verify("u1", sealed, wrong)
        assert not await totp.verify("u1", sealed, totp.code("u1", sealed))  # even the right one
        other = totp.enroll("u2", "ben").sealed
        assert await totp.verify("u2", other, totp.code("u2", other))  # others aren't locked

    async def test_a_right_code_forgives_the_wrong_ones(self, clock: Clock) -> None:
        totp = make(max_attempts=3)
        sealed = totp.enroll("u1", "ana").sealed
        wrong = f"{(int(totp.code('u1', sealed)) + 1) % 10**6:06d}"
        for _ in range(2):
            await totp.verify("u1", sealed, wrong)
        assert await totp.verify("u1", sealed, totp.code("u1", sealed))
        clock.now += 30
        for _ in range(2):
            await totp.verify("u1", sealed, wrong)
        assert await totp.verify("u1", sealed, totp.code("u1", sealed))

    @pytest.mark.usefixtures("clock")
    async def test_eight_digits_and_sha256(self) -> None:
        totp = make(digits=8, algorithm="SHA256", period=60)
        enrollment = totp.enroll("u1", "ana")
        assert "digits=8" in enrollment.uri
        assert "algorithm=SHA256" in enrollment.uri
        code = totp.code("u1", enrollment.sealed)
        assert len(code) == 8
        assert await totp.verify("u1", enrollment.sealed, code)

    @pytest.mark.usefixtures("clock")
    async def test_a_failing_backend_accepts_nothing(self) -> None:
        class Down(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                raise ConnectionError("down")

        totp = make(cache=Cache(Down()))
        sealed = totp.enroll("u1", "ana").sealed
        assert not await totp.verify("u1", sealed, totp.code("u1", sealed))

    @pytest.mark.usefixtures("clock")
    async def test_counted(self) -> None:
        reader = InMemoryMetricReader()
        cache = Cache(InMemoryBackend(), meter_provider=MeterProvider(metric_readers=[reader]))
        totp = make(cache=cache)
        sealed = totp.enroll("u1", "ana").sealed
        code = totp.code("u1", sealed)
        await totp.verify("u1", sealed, code)
        await totp.verify("u1", sealed, code)
        await totp.verify("u1", sealed, "abc")
        counted = points(reader, "fastapi_alcazaba.totp.verifications")
        outcomes = {attributes["fastapi_alcazaba.outcome"]: value for attributes, value in counted}
        assert outcomes == {"success": 1, "replayed": 1, "malformed": 1}


class TestRecoveryCodes:
    def test_their_form(self) -> None:
        recovery = make().recovery_codes("u1")
        assert len(recovery.codes) == len(set(recovery.codes)) == 10
        for code in recovery.codes:
            assert len(code) == 11
            assert code[5] == "-"
            assert set(code.replace("-", "")) <= set("ABCDEFGHJKMNPQRSTUVWXYZ23456789")
        assert all(code not in "".join(recovery.digests) for code in recovery.codes)

    async def test_each_works_once(self) -> None:
        totp = make()
        recovery = totp.recovery_codes("u1", count=3)
        code = recovery.codes[1]
        assert await totp.use_recovery_code("u1", code, recovery.digests) == recovery.digests[1]
        # Until the app deletes it, the same code is refused: of two requests, one wins.
        assert await totp.use_recovery_code("u1", code, recovery.digests) is None
        remaining = [d for d in recovery.digests if d != recovery.digests[1]]
        assert await totp.use_recovery_code("u1", recovery.codes[0], remaining) == remaining[0]

    async def test_typed_as_people_type_them(self) -> None:
        totp = make()
        recovery = totp.recovery_codes("u1", count=1)
        typed = recovery.codes[0].lower().replace("-", " ")
        assert await totp.use_recovery_code("u1", typed, recovery.digests) == recovery.digests[0]

    async def test_another_users_codes(self) -> None:
        totp = make()
        recovery = totp.recovery_codes("u1", count=1)
        assert await totp.use_recovery_code("u2", recovery.codes[0], recovery.digests) is None

    @pytest.mark.usefixtures("clock")
    async def test_wrong_ones_count_as_guesses(self) -> None:
        totp = make(max_attempts=2)
        recovery = totp.recovery_codes("u1", count=1)
        sealed = totp.enroll("u1", "ana").sealed
        for _ in range(2):
            assert await totp.use_recovery_code("u1", "AAAAA-AAAAA", recovery.digests) is None
        assert await totp.use_recovery_code("u1", recovery.codes[0], recovery.digests) is None
        assert not await totp.verify("u1", sealed, totp.code("u1", sealed))

    async def test_they_survive_a_rotation(self) -> None:
        recovery = make(secret=SECRET).recovery_codes("u1", count=1)
        rotated = make(secret=["a-new-secret-of-at-least-32-bytes!!", SECRET])
        assert await rotated.use_recovery_code("u1", recovery.codes[0], recovery.digests)

    async def test_a_failing_backend_accepts_none(self) -> None:
        class Down(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                raise ConnectionError("down")

        totp = make(cache=Cache(Down()))
        recovery = totp.recovery_codes("u1", count=1)
        assert await totp.use_recovery_code("u1", recovery.codes[0], recovery.digests) is None

    @pytest.mark.usefixtures("clock")
    async def test_guesses_not_forgiven_still_let_the_right_code_in(self) -> None:
        """Deleting the count of guesses fails: they expire by themselves."""

        class NoDeletes(InMemoryBackend):
            async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
                raise ConnectionError("down")

        totp = make(cache=Cache(NoDeletes()))
        sealed = totp.enroll("u1", "ana").sealed
        assert await totp.verify("u1", sealed, totp.code("u1", sealed))
        recovery = totp.recovery_codes("u1", count=1)
        assert await totp.use_recovery_code("u1", recovery.codes[0], recovery.digests)

    def test_count(self) -> None:
        with pytest.raises(ValueError, match="count"):
            make().recovery_codes("u1", count=0)


def step_up_app(totp: FastAPITOTP) -> TestClient:
    app = FastAPI()

    def user_id(x_user: Annotated[str | None, Header()] = None) -> str | None:
        return x_user

    @app.post("/mfa")
    async def mfa(request: Request, user: Annotated[str, Depends(user_id)]) -> None:
        totp.mark(request, user)  # after verify() accepted their code

    @app.delete("/account", dependencies=[Depends(totp.require_recent(user_id, max_age=300))])
    async def delete_account() -> str:
        return "deleted"

    Cache(InMemoryBackend()).install(app)
    app.add_middleware(ServerSessionMiddleware, cookie_name="session", secure=False)
    return TestClient(app)


class WithoutLocks(InMemoryBackend):
    supports_locks = False


class TestBackendsWithoutLocks:
    """Each code is marked used with a lock: without them, every right code was refused."""

    def test_refused_when_given(self) -> None:
        with pytest.raises(NotImplementedError, match="can't hold locks.*shipped .* can"):
            make(cache=Cache(WithoutLocks()))

    async def test_refused_when_resolved(self) -> None:
        FastAPICache.init(WithoutLocks())
        totp = make(cache=None)
        enrollment = totp.enroll("ana", label="ana@example.com")
        with pytest.raises(NotImplementedError, match="can't hold locks"):
            await totp.verify("ana", enrollment.sealed, totp.code("ana", enrollment.sealed))
        recovery = totp.recovery_codes("ana")
        with pytest.raises(NotImplementedError, match="can't hold locks"):
            await totp.use_recovery_code("ana", recovery.codes[0], recovery.digests)

    async def test_memcached_holds_them(self) -> None:
        totp = make(cache=Cache(MemcachedBackend(FakeMemcache())))  # type: ignore[arg-type]
        sealed = totp.enroll("ana", label="ana@example.com").sealed
        code = totp.code("ana", sealed)
        assert await totp.verify("ana", sealed, code)
        assert not await totp.verify("ana", sealed, code)
        recovery = totp.recovery_codes("ana")
        used = await totp.use_recovery_code("ana", recovery.codes[0], recovery.digests)
        assert used == recovery.digests[0]
        assert await totp.use_recovery_code("ana", recovery.codes[0], recovery.digests) is None


class TestStepUp:
    def test_a_recent_second_factor(self, clock: Clock) -> None:
        client = step_up_app(make())
        refused = client.delete("/account", headers={"X-User": "u1"})
        assert (refused.status_code, refused.json()) == (403, {"detail": "Second factor required"})
        client.post("/mfa", headers={"X-User": "u1"})
        before = client.cookies["session"]
        client.post("/mfa", headers={"X-User": "u1"})
        assert client.cookies["session"] != before  # each time a new session id
        assert client.delete("/account", headers={"X-User": "u1"}).json() == "deleted"
        clock.now += 301
        assert client.delete("/account", headers={"X-User": "u1"}).status_code == 403

    @pytest.mark.usefixtures("clock")
    def test_someone_elses(self) -> None:
        """Another user in the same browser session doesn't inherit it."""
        client = step_up_app(make())
        client.post("/mfa", headers={"X-User": "u1"})
        assert client.delete("/account", headers={"X-User": "u2"}).status_code == 403
        assert client.delete("/account").status_code == 401  # nobody

    def test_without_sessions(self) -> None:
        totp = make()
        app = FastAPI()
        recent = Depends(totp.require_recent(lambda: "u1"))
        app.delete("/account", dependencies=[recent])(lambda: None)
        with pytest.raises(RuntimeError, match="needs ServerSessionMiddleware"):
            TestClient(app).delete("/account")
        with pytest.raises(ValueError, match="max_age"):
            totp.require_recent(lambda: "u1", max_age=0)


@pytest.mark.usefixtures("clock")
async def test_the_audit_trail() -> None:
    sink = MemoryAuditSink()
    totp = make(audit=FastAPIAudit(sinks=[sink]))
    sealed = totp.enroll("u1", "ana").sealed
    code = totp.code("u1", sealed)
    await totp.verify("u1", sealed, code)
    await totp.verify("u1", sealed, code)
    recovery = totp.recovery_codes("u1", count=1)
    await totp.use_recovery_code("u1", recovery.codes[0], recovery.digests)
    assert [(e.action, e.outcome, e.actor, e.reason) for e in sink.events] == [
        ("auth.mfa", "success", "u1", None),
        ("auth.mfa", "failure", "u1", "replayed"),
        ("auth.recovery_code", "success", "u1", None),
    ]
    assert code not in json.dumps([e.to_dict() for e in sink.events])
