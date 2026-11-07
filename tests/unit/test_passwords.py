"""FastAPIPasswords: scrypt hashes in PHC's format, a lockout per account, the same time for
unknown accounts, new passwords as NIST says, leaked ones, and reset tokens used once."""

import asyncio
import hashlib
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from fastapi_alcazaba import Cache, FastAPIAudit
from fastapi_alcazaba.audit import MemoryAuditSink
from fastapi_alcazaba.auth import (
    FastAPIPasswords,
    PasswordBlocklist,
    PasswordCheck,
    PasswordStrength,
    PwnedPasswords,
    ScryptParameters,
)
from fastapi_alcazaba.auth import passwords as passwords_module
from fastapi_alcazaba.auth.passwords import MAX_PASSWORD, PROBLEMS
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.core.errors import BackendUnavailable

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

    @pytest.mark.parametrize(
        "password",
        ["zurbaranbelen1984", "BELÉN is the best 84", "my handle anabel_z forever"],
    )
    async def test_the_words_of_the_users_data(self, password: str) -> None:
        user_inputs = ["Ana Belén Zurbarán", "anabel_z.84@example.com"]
        assert await make(min_length=8).check(password, user_inputs=user_inputs) == [
            "contains_user_input"
        ]

    async def test_not_the_domain_of_the_address(self) -> None:
        user_inputs = ["ana@gmail.com"]
        assert await make().check("my gmail passphrase is long", user_inputs=user_inputs) == []

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
        with pytest.raises(BackendUnavailable):
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

    async def test_a_planted_token_isnt_taken_with_a_signing_key(self) -> None:
        """Whoever can write to the backend, without the app's key, can't make a token of their
        own work for another user: the records are sealed, and those the app didn't seal are
        unknown."""
        backend = InMemoryBackend()
        cache = Cache(backend, prefix="app", signing_key="k" * 40)
        passwords = make(cache=cache)
        token = "a token of the writer's own"  # noqa: S105
        digest = hashlib.sha256(token.encode()).hexdigest()
        await backend.set(passwords._key(cache, "reset", digest), b"victim", 3600)  # pylint: disable=protected-access
        await backend.set(passwords._key(cache, "reset-of", "victim"), digest.encode(), 3600)  # pylint: disable=protected-access
        assert await passwords.use_reset_token(token) is None
        genuine = await passwords.reset_token("victim")
        assert await passwords.use_reset_token(genuine) == "victim"


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


SURROGATE = "\ud800 a password JSON lets through"  # noqa: S105  # valid JSON, not UTF-8


class TestEvaluate:
    async def test_without_any_check_set_up(self) -> None:
        result = await make().evaluate("just a long passphrase of words")
        assert result == PasswordCheck(checks=("length", "characters"))
        assert result.ok
        assert result.score is None

    async def test_every_problem_at_once_in_order(self) -> None:
        passwords = make(strength=PasswordStrength(), blocklist=PasswordBlocklist(["ana"]))
        result = await passwords.evaluate("ana", user_inputs=["ana@example.com"])
        assert result.problems == ("too_short", "blocklisted", "weak")
        assert set(result.problems) <= set(PROBLEMS)
        assert not result.ok
        assert result.checks == ("length", "characters", "user_inputs", "blocklist", "strength")

    async def test_check_gives_the_problems(self) -> None:
        passwords = make(blocklist=PasswordBlocklist(["password1234567890"]))
        assert await passwords.check("password1234567890") == ["blocklisted"]
        assert await passwords.check("just a long passphrase of words") == []

    async def test_lone_surrogates_are_refused_and_never_raise(self) -> None:
        passwords = make()
        assert (await passwords.evaluate(SURROGATE)).problems == ("invalid_characters",)
        stored = await passwords.hash(SURROGATE)  # an app that didn't check first: no 500
        assert await passwords.verify("u1", SURROGATE, stored)
        assert not await passwords.verify("u1", SURROGATE, await passwords.hash(PASSWORD))
        assert await PwnedPasswords(client=pwned_api({})).count(SURROGATE) == 0

    async def test_user_inputs_are_a_list(self) -> None:
        with pytest.raises(TypeError, match="list of strings"):
            await make().evaluate(PASSWORD, user_inputs="ana@example.com")
        with pytest.raises(TypeError, match="list of strings"):
            PasswordStrength().measure(PASSWORD, user_inputs="ana@example.com")

    async def test_the_result_never_holds_the_password(self) -> None:
        passwords = make(strength=PasswordStrength(), blocklist=PasswordBlocklist([PASSWORD]))
        for password in (PASSWORD, "Tr0ub4dor&3 is mine"):
            result = await passwords.evaluate(password)
            assert password not in repr(result)

    async def test_a_megabyte(self) -> None:
        passwords = make(strength=PasswordStrength(), blocklist=PasswordBlocklist(["x"]))
        started = time.perf_counter()
        result = await passwords.evaluate("Tr0ub4dor&3 " * 100_000)
        assert result.problems == ("too_long",)
        assert time.perf_counter() - started < 2


class TestStrength:
    @pytest.mark.parametrize(
        ("password", "score"),
        [
            ("password", 0),
            ("iloveyou1", 1),
            ("kitten-cat", 2),
            ("sunrise-velvet", 3),
            ("correct horse battery staple", 4),
        ],
    )
    def test_zxcvbns_scores(self, password: str, score: int) -> None:
        assert PasswordStrength().measure(password)[0] == score

    @pytest.mark.parametrize("min_score", [0, 1, 2, 3, 4])
    async def test_the_minimum_score(self, min_score: int) -> None:
        passwords = make(min_length=8, strength=PasswordStrength(min_score=min_score))
        for password, score in [("password", 0), ("kitten-cat", 2), (PASSWORD, 4)]:
            result = await passwords.evaluate(password)
            assert result.score == score
            assert ("weak" in result.problems) is (score < min_score)

    def test_advice_for_weak_ones_only(self) -> None:
        score, advice = PasswordStrength().measure("p@ssw0rd123")
        assert score == 1
        assert "This is similar to a commonly used password." in advice
        assert PasswordStrength().measure(PASSWORD) == (4, ())

    @pytest.mark.parametrize(
        ("password", "user_inputs"),
        [
            ("mudarramachuca", ["Antonio Mudarra Machuca"]),  # the words of a name
            ("acmewidgets", ["sales@acmewidgets.com"]),  # an e-mail address's domain
            ("zurbaran1984", ["zurbaran@example.com"]),  # its local part
            ("zurbaranbelen", ["Belén Zurbarán"]),  # without the accents
        ],
    )
    def test_the_users_data_is_guessable(self, password: str, user_inputs: list[str]) -> None:
        strength = PasswordStrength()
        alone, _ = strength.measure(password)
        known, _ = strength.measure(password, user_inputs=user_inputs)
        assert alone >= 3
        assert known <= 1

    def test_past_72_characters_only_the_first_are_scored(self) -> None:
        strength = PasswordStrength()
        assert strength.measure("a" * 1000)[0] == 0
        assert strength.measure("a" * 72 + PASSWORD)[0] == 0
        assert strength.measure(PASSWORD + "a" * 1000)[0] == 4

    async def test_one_call_at_a_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """zxcvbn keeps a call's user inputs in a module global: two at once would mix them."""
        strength = PasswordStrength()
        real = strength._zxcvbn  # pylint: disable=protected-access

        def locked(password: str, user_inputs: list[str]) -> dict[str, Any]:
            assert passwords_module._ZXCVBN_LOCK.locked()  # pylint: disable=protected-access
            return real(password, user_inputs=user_inputs)  # type: ignore[no-any-return]

        monkeypatch.setattr(strength, "_zxcvbn", locked)
        passwords = make(strength=strength)
        names = [f"user{index}name{index}" for index in range(20)]
        results = await asyncio.gather(
            *(passwords.evaluate(f"{name}{name}", user_inputs=[name]) for name in names)
        )
        alone = [strength.measure(f"{name}{name}", user_inputs=[name]) for name in names]
        assert [(result.score, result.suggestions) for result in results] == alone

    @pytest.mark.parametrize("min_score", [-1, 5, 3.0, True, "3"])
    def test_the_minimum_is_0_to_4(self, min_score: Any) -> None:
        with pytest.raises(ValueError, match="min_score"):
            PasswordStrength(min_score=min_score)

    def test_without_zxcvbn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "zxcvbn", None)
        with pytest.raises(ImportError, match=r"fastapi-alcazaba\[password-strength\]"):
            PasswordStrength()


class TestBlocklist:
    @pytest.mark.parametrize(
        "password",
        ["Acme2024", "acme2024", "ACME2024", "  acme2024\n", "ＡＣＭＥ２０２４"],
    )
    async def test_normalized_matches(self, password: str) -> None:
        passwords = make(min_length=8, blocklist=PasswordBlocklist(["Acme2024"]))
        assert (await passwords.evaluate(password)).problems == ("blocklisted",)

    def test_only_whole_passwords(self) -> None:
        blocklist = PasswordBlocklist(["acme"])
        assert "acme" in blocklist
        assert "acme rocks every day" not in blocklist
        assert 1234 not in blocklist

    def test_blank_entries_are_skipped(self) -> None:
        blocklist = PasswordBlocklist(["", "  ", "\n", "Acme", "acme "])
        assert len(blocklist) == 1
        assert "" not in blocklist
        assert " " not in blocklist

    def test_from_files(self, tmp_path: Path) -> None:
        common = tmp_path / "common.txt"
        common.write_bytes(b"123456\r\npassword\r\n\r\nqwerty\r\n")
        ours = tmp_path / "ours.txt"
        ours.write_text("Alcazaba\nalcázaba2024\n", encoding="utf-8")
        blocklist = PasswordBlocklist.from_files(common, str(ours))
        assert len(blocklist) == 5
        for password in ("123456", "QWERTY", "alcazaba", "ALCÁZABA2024"):
            assert password in blocklist

    def test_files_it_cant_read(self, tmp_path: Path) -> None:
        with pytest.raises(OSError, match="missing"):
            PasswordBlocklist.from_files(tmp_path / "missing.txt")
        latin1 = tmp_path / "latin1.txt"
        latin1.write_bytes("contraseña\n".encode("latin-1"))
        with pytest.raises(UnicodeDecodeError):
            PasswordBlocklist.from_files(latin1)
        assert "contraseña" in PasswordBlocklist.from_files(latin1, encoding="latin-1")


def answering(status: int = 200, text: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=text))
    )


def raising(error: type[httpx.TransportError]) -> httpx.AsyncClient:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error("failed", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(fail))


SHA1 = hashlib.sha1(PASSWORD.encode(), usedforsecurity=False).hexdigest().upper()

UNAVAILABLE = {
    "down": lambda: raising(httpx.ConnectError),
    "timeout": lambda: raising(httpx.ReadTimeout),
    "503": lambda: answering(503, "Service Unavailable"),
    "429": lambda: answering(429),
    "a proxy's page": lambda: answering(200, "<html><body>Log in to the Wi-Fi</body></html>"),
    "empty": lambda: answering(200, ""),
    "a huge count": lambda: answering(200, f"{SHA1[5:]}:{'9' * 5000}"),
    "other digits": lambda: answering(200, f"{SHA1[5:]}:٤٢"),
    "a short line": lambda: answering(200, "ABCDEF:1"),
}


def assert_no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Neither the password nor its hash in any log; nor the prefix sent in Alcazaba's (httpx2
    logs each request's URL at INFO, with it: the guide says to keep it at WARNING)."""
    for secret in (PASSWORD, SHA1, SHA1[5:]):
        assert secret not in caplog.text
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("fastapi_alcazaba")]
    assert not any(SHA1[:5] in message for message in ours)


class TestPwnedChecks:
    async def test_leaked_and_not(self) -> None:
        passwords = make(pwned=PwnedPasswords(client=pwned_api({"password1234567890": 4242})))
        leaked = await passwords.evaluate("password1234567890")
        assert leaked.problems == ("pwned",)
        assert "pwned" in leaked.checks
        fine = await passwords.evaluate(PASSWORD)
        assert fine.ok
        assert fine.checks == ("length", "characters", "pwned")
        assert fine.unavailable == ()

    async def test_lookup_counts(self) -> None:
        pwned = PwnedPasswords(client=pwned_api({PASSWORD: 7}))
        assert await pwned.lookup(PASSWORD) == 7
        assert await pwned.lookup("not leaked, nor anywhere") == 0

    async def test_lowercase_hex_is_read(self) -> None:
        text = f"{SHA1[5:].lower()}:3\n\n{'0' * 35}:0\n"  # blank lines are skipped
        assert await PwnedPasswords(client=answering(200, text)).count(PASSWORD) == 3

    async def test_its_own_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=f"{SHA1[5:]}:2")

        real = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **options: real(transport=httpx.MockTransport(answer), **options),
        )
        assert await PwnedPasswords(timeout=1.5).count(PASSWORD) == 2
        assert str(seen[0].url) == f"https://api.pwnedpasswords.com/range/{SHA1[:5]}"
        assert seen[0].extensions["timeout"]["read"] == 1.5

    async def test_the_timeout_reaches_the_call(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="0" * 35 + ":0")

        client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
        await PwnedPasswords(client=client, timeout=0.5).count(PASSWORD)
        assert seen[0].extensions["timeout"]["read"] == 0.5

    @pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
    def test_a_timeout_above_0(self, timeout: float) -> None:
        with pytest.raises(ValueError, match="timeout"):
            PwnedPasswords(timeout=timeout)

    @pytest.mark.parametrize("case", list(UNAVAILABLE))
    async def test_fail_open(self, case: str, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        pwned = PwnedPasswords(client=UNAVAILABLE[case]())
        assert await pwned.lookup(PASSWORD) is None
        assert await pwned.count(PASSWORD) == 0
        result = await make(pwned=pwned).evaluate(PASSWORD)
        assert result.ok
        assert result.unavailable == ("pwned",)
        assert "pwned" not in result.checks
        assert "Couldn't check a password against the leaks" in caplog.text
        assert_no_secret_logged(caplog)

    @pytest.mark.parametrize("case", list(UNAVAILABLE))
    async def test_fail_closed(self, case: str, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(logging.DEBUG)
        pwned = PwnedPasswords(client=UNAVAILABLE[case](), fail_open=False)
        with pytest.raises(BackendUnavailable) as raised:
            await make(pwned=pwned).evaluate(PASSWORD)
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__
        for secret in (PASSWORD, SHA1, SHA1[:5], SHA1[5:]):
            assert secret not in str(raised.value)
        assert_no_secret_logged(caplog)

    async def test_no_call_without_pwned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def no_network(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("a call without PwnedPasswords")

        monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
        passwords = make(strength=PasswordStrength(), blocklist=PasswordBlocklist(["x"]))
        assert (await passwords.evaluate(PASSWORD)).ok
