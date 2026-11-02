"""FastAPIPasswords: scrypt hashes in PHC's format, a lockout per account, the same time for
unknown accounts, new passwords as NIST says, leaked ones, and reset tokens used once."""

import hashlib
import time
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba import Cache, FastAPIAudit
from fastapi_alcazaba.audit import MemoryAuditSink
from fastapi_alcazaba.auth import FastAPIPasswords, PwnedPasswords, ScryptParameters
from fastapi_alcazaba.auth.passwords import MAX_PASSWORD
from fastapi_alcazaba.backends.inmemory import InMemoryBackend

CHEAP = ScryptParameters(log_n=14)  # fast enough for tests; OWASP's is 2^17
PEPPER = "a-pepper-of-at-least-thirty-two-bytes"  # noqa: S105  # the tests' own
PASSWORD = "correct horse battery staple"  # noqa: S105


def make(**options: Any) -> FastAPIPasswords:
    return FastAPIPasswords(**{"parameters": CHEAP, "cache": Cache(InMemoryBackend()), **options})


class TestHashes:
    async def test_phc_format(self) -> None:
        stored = await make().hash(PASSWORD)
        assert stored.startswith("$scrypt$ln=14,r=8,p=1$")
        assert PASSWORD not in stored
        assert stored != await make().hash(PASSWORD)  # a salt each

    async def test_verify(self) -> None:
        passwords = make()
        stored = await passwords.hash(PASSWORD)
        assert await passwords.verify("u1", PASSWORD, stored)
        assert not await passwords.verify("u1", PASSWORD + "!", stored)

    async def test_the_default_parameters_are_owasps(self) -> None:
        assert (ScryptParameters().log_n, ScryptParameters().r, ScryptParameters().p) == (17, 8, 1)

    async def test_needs_rehash(self) -> None:
        weak = await make().hash(PASSWORD)
        stronger = make(parameters=ScryptParameters(log_n=15))
        assert stronger.needs_rehash(weak)
        assert not make().needs_rehash(weak)
        assert stronger.needs_rehash("$2b$12$a bcrypt hash")
        assert await stronger.verify("u1", PASSWORD, weak)  # verified with its own parameters

    async def test_a_pepper(self) -> None:
        peppered = make(pepper=PEPPER)
        stored = await peppered.hash(PASSWORD)
        assert await peppered.verify("u1", PASSWORD, stored)
        assert not await make().verify("u1", PASSWORD, stored)  # without the pepper, nothing
        assert not await make(pepper=PEPPER[::-1]).verify("u1", PASSWORD, stored)

    @pytest.mark.parametrize(
        "stored",
        ["", "plain", "$scrypt$ln=99,r=8,p=1$c2FsdA$aGFzaA", "$scrypt$ln=14,r=8,p=1$!!$??"],
    )
    async def test_malformed_hashes(self, stored: str) -> None:
        assert not await make().verify("u1", PASSWORD, stored)

    async def test_too_long(self) -> None:
        passwords = make()
        with pytest.raises(ValueError, match="1024 characters at most"):
            await passwords.hash("x" * (MAX_PASSWORD + 1))
        stored = await passwords.hash("x" * MAX_PASSWORD)
        assert await passwords.verify("u1", "x" * MAX_PASSWORD, stored)
        assert not await passwords.verify("u1", "x" * (MAX_PASSWORD + 1), stored)

    async def test_unknown_accounts_take_as_long(self) -> None:
        passwords = make()
        stored = await passwords.hash(PASSWORD)
        start = time.perf_counter()
        await passwords.verify("u1", "wrong", stored)
        known = time.perf_counter() - start
        start = time.perf_counter()
        assert not await passwords.verify(None, "wrong", None)
        unknown = time.perf_counter() - start
        assert unknown > known / 3  # a hash, not nothing


class TestLockout:
    async def test_after_max_attempts(self) -> None:
        passwords = make(max_attempts=3)
        stored = await passwords.hash(PASSWORD)
        for _ in range(3):
            assert not await passwords.verify("u1", "guess", stored)
        assert not await passwords.verify("u1", PASSWORD, stored)  # even the right one
        other = await passwords.hash(PASSWORD)
        assert await passwords.verify("u2", PASSWORD, other)  # other accounts aren't locked

    async def test_a_right_password_forgives(self) -> None:
        passwords = make(max_attempts=3)
        stored = await passwords.hash(PASSWORD)
        for _ in range(2):
            await passwords.verify("u1", "guess", stored)
        assert await passwords.verify("u1", PASSWORD, stored)
        for _ in range(2):
            await passwords.verify("u1", "guess", stored)
        assert await passwords.verify("u1", PASSWORD, stored)

    async def test_a_failing_backend_refuses(self) -> None:
        class Down(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                raise ConnectionError("down")

        passwords = make(cache=Cache(Down()))
        stored = await passwords.hash(PASSWORD)
        assert not await passwords.verify("u1", PASSWORD, stored)


class TestNewPasswords:
    @pytest.mark.parametrize(
        ("password", "problems"),
        [
            ("short", ["too_short"]),
            ("x" * 15, []),
            ("x" * (MAX_PASSWORD + 1), ["too_long"]),
            ("my name is anabel and this is long", ["contains_user_input"]),
            ("ANA@EXAMPLE.COM is my password, long", ["contains_user_input"]),
            ("just a long passphrase of words", []),
        ],
    )
    async def test_check(self, password: str, problems: list[str]) -> None:
        found = await make().check(password, user_inputs=["anabel", "ana@example.com"])
        assert found == problems

    async def test_short_inputs_dont_count(self) -> None:
        assert await make().check("a long passphrase with ab in it", user_inputs=["ab"]) == []

    def test_configuration(self) -> None:
        with pytest.raises(ValueError, match="min_length"):
            make(min_length=4)
        with pytest.raises(ValueError, match="pepper"):
            make(pepper="short")
        with pytest.raises(ValueError, match="scrypt's parameters"):
            ScryptParameters(log_n=10)


def pwned_api(leaked: dict[str, int]) -> httpx.AsyncClient:
    """Stands in for the Pwned Passwords range API: the suffixes of a prefix, padded."""
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        prefix = request.url.path.rsplit("/", 1)[-1]
        lines = ["0" * 35 + ":0"]  # padding
        for password, count in leaked.items():
            sha1 = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
            if sha1.startswith(prefix):
                lines.append(f"{sha1[5:]}:{count}")
        return httpx.Response(200, text="\r\n".join(lines))

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    client.seen = seen  # type: ignore[attr-defined]
    return client


class TestPwned:
    async def test_a_leaked_password(self) -> None:
        client = pwned_api({"password1234567890": 4242})
        passwords = make(pwned=PwnedPasswords(client=client))
        assert await passwords.check("password1234567890") == ["pwned"]
        assert await passwords.check("a passphrase nobody leaked yet") == []

    async def test_only_five_characters_of_the_hash_go_out(self) -> None:
        client = pwned_api({})
        await PwnedPasswords(client=client).count(PASSWORD)
        assert len(client.seen) == 1  # type: ignore[attr-defined]
        request = client.seen[0]  # type: ignore[attr-defined]
        sha1 = hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest().upper()
        assert request.url.path == f"/range/{sha1[:5]}"
        assert request.headers["Add-Padding"] == "true"
        assert PASSWORD not in str(request.url)

    async def test_not_checked_when_already_refused(self) -> None:
        client = pwned_api({})
        await make(pwned=PwnedPasswords(client=client)).check("short")
        assert client.seen == []  # type: ignore[attr-defined]

    async def test_the_api_down(self) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(down))
        assert await PwnedPasswords(client=client).count(PASSWORD) == 0
        with pytest.raises(httpx.ConnectError):
            await PwnedPasswords(client=client, fail_open=False).count(PASSWORD)


class TestResets:
    async def test_a_token_works_once(self) -> None:
        passwords = make()
        token = await passwords.reset_token("u1")
        assert len(token) >= 43
        assert await passwords.use_reset_token(token) == "u1"
        assert await passwords.use_reset_token(token) is None

    async def test_a_new_token_replaces_the_last(self) -> None:
        passwords = make()
        old = await passwords.reset_token("u1")
        new = await passwords.reset_token("u1")
        assert await passwords.use_reset_token(old) is None
        assert await passwords.use_reset_token(new) == "u1"

    async def test_tokens_of_others_are_theirs(self) -> None:
        passwords = make()
        ana, ben = await passwords.reset_token("u1"), await passwords.reset_token("u2")
        assert await passwords.use_reset_token(ben) == "u2"
        assert await passwords.use_reset_token(ana) == "u1"

    @pytest.mark.parametrize("token", ["", "x" * 101, "made-up"])
    async def test_made_up_tokens(self, token: str) -> None:
        assert await make().use_reset_token(token) is None

    async def test_the_backend_holds_digests(self) -> None:
        backend = InMemoryBackend()
        passwords = make(cache=Cache(backend))
        token = await passwords.reset_token("u1")
        stored = b"".join(value.data for value in backend._state["passwords"].values())  # pylint: disable=protected-access
        assert token.encode() not in stored


async def test_the_audit_trail() -> None:
    sink = MemoryAuditSink()
    passwords = make(audit=FastAPIAudit(sinks=[sink]))
    stored = await passwords.hash(PASSWORD)
    await passwords.verify("u1", PASSWORD, stored)
    await passwords.verify("u1", "guess", stored)
    await passwords.use_reset_token(await passwords.reset_token("u1"))
    assert [(e.action, e.outcome, e.actor, e.reason) for e in sink.events] == [
        ("auth.password", "success", "u1", None),
        ("auth.password", "failure", "u1", "wrong"),
        ("auth.password_reset", "success", "u1", None),
    ]
    assert PASSWORD not in str([e.to_dict() for e in sink.events])
