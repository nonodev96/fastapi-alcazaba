"""A trail an auditor verifies without being able to forge it: Ed25519 signatures, key rotation,
checkpoints that reveal the newest events removed, and the command-line verifier."""

# pylint: disable=protected-access  # tests look at the internals they check

import dataclasses
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from fastapi_alcazaba import Cache
from fastapi_alcazaba.audit import (
    CHECKPOINT,
    AuditEvent,
    FastAPIAudit,
    MemoryAuditSink,
    generate_audit_key_pair,
    last_checkpoint,
    verify_audit_trail,
)
from fastapi_alcazaba.audit import trail as audit_module
from fastapi_alcazaba.audit.keys import audit_keys
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.cli import NOT_DONE, USAGE, main

SECRET = "an-hmac-secret-of-at-least-32-bytes!"  # noqa: S105  # the tests' own


async def write(audit: FastAPIAudit, count: int, action: str = "user.update") -> list[AuditEvent]:
    return [await audit.log(action, actor=f"user-{n}") for n in range(count)]


class TestEd25519:
    async def test_the_public_key_verifies_and_cant_sign(self) -> None:
        private, public = generate_audit_key_pair()
        trail = MemoryAuditSink()
        events = await write(FastAPIAudit([trail], signing_key=private), 3)
        assert all((event.signature or "").startswith("ed25519:") for event in events)
        assert verify_audit_trail(events, public) == []
        with pytest.raises(ValueError, match="signs: a private key or a secret"):
            FastAPIAudit(signing_key=public)

    async def test_altered_and_forged(self) -> None:
        private, public = generate_audit_key_pair()
        events = await write(FastAPIAudit([MemoryAuditSink()], signing_key=private), 2)
        altered = {**events[1].to_dict(), "actor": "mallory"}
        other_private, _ = generate_audit_key_pair()
        forged = (await write(FastAPIAudit([MemoryAuditSink()], signing_key=other_private), 1))[0]
        garbled = {**events[0].to_dict(), "signature": events[0].signature + "!"}  # type: ignore[operator]
        problems = verify_audit_trail([events[0], altered, forged, garbled], public)
        assert len(problems) == 3
        assert all("doesn't match its signature" in problem for problem in problems)

    async def test_key_objects(self) -> None:
        private = ed25519.Ed25519PrivateKey.generate()
        events = await write(FastAPIAudit([MemoryAuditSink()], signing_key=private), 2)
        assert verify_audit_trail(events, private.public_key()) == []
        assert verify_audit_trail(events, private) == []  # a private key verifies too


class TestRotation:
    async def test_events_of_every_key_verify_with_all_of_them(self) -> None:
        old_private, old_public = generate_audit_key_pair()
        new_private, new_public = generate_audit_key_pair()
        trail = MemoryAuditSink()
        before = await write(FastAPIAudit([trail], signing_key=old_private), 2)
        after = await write(FastAPIAudit([trail], signing_key=[new_private, old_private]), 2)
        assert verify_audit_trail([*before, *after], [new_public, old_public]) == []
        problems = verify_audit_trail([*before, *after], new_public)  # the old public key lost
        assert len(problems) == 2
        kids = {(event.signature or "").split(":")[1] for event in [*before, *after]}
        assert len(kids) == 2

    async def test_from_hmac_to_ed25519(self) -> None:
        private, public = generate_audit_key_pair()
        trail = MemoryAuditSink()
        before = await write(FastAPIAudit([trail], signing_key=SECRET), 1)
        after = await write(FastAPIAudit([trail], signing_key=[private, SECRET]), 1)
        assert verify_audit_trail([*before, *after], [public, SECRET]) == []

    @pytest.mark.parametrize(
        ("keys", "error", "message"),
        [
            ([], ValueError, "at least one key"),
            ([SECRET, SECRET], ValueError, "the same key twice"),
            (42, TypeError, "not an audit key"),
            ("short", ValueError, "at least 32 bytes"),
            (
                "-----BEGIN PUBLIC KEY-----\nnot base64\n-----END PUBLIC KEY-----\n",
                ValueError,
                "PEM",
            ),
        ],
    )
    def test_invalid_keys(self, keys: Any, error: type[Exception], message: str) -> None:
        with pytest.raises(error, match=message):
            audit_keys(keys)

    def test_only_ed25519(self) -> None:
        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        for pem in (
            rsa_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            rsa_key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ),
        ):
            with pytest.raises(ValueError, match="Ed25519"):
                audit_keys(pem)

    async def test_every_form_of_a_public_key_is_one(self) -> None:
        """A public key in DER, OpenSSH, raw PEM with text before it: read as the key it is,
        never as an HMAC secret, which anybody holding it could sign with."""
        private = ed25519.Ed25519PrivateKey.generate()
        public = private.public_key()
        events = await write(FastAPIAudit([MemoryAuditSink()], signing_key=private), 2)
        forms = [
            public.public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            ),
            public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH),
            b"the auditor's key:\n"
            + public.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            ),
        ]
        for form in forms:
            (key,) = audit_keys(form)
            assert (key.algorithm, key.can_sign) == ("ed25519", False)
            assert verify_audit_trail(events, form) == []
            with pytest.raises(ValueError, match="signs: a private key or a secret"):
                FastAPIAudit(signing_key=form)
        der_private = private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        assert audit_keys(der_private)[0].can_sign
        rsa_der = (
            rsa.generate_private_key(public_exponent=65537, key_size=2048)
            .public_key()
            .public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
        with pytest.raises(ValueError, match="Ed25519"):
            audit_keys(rsa_der)
        with pytest.raises(ValueError, match="OpenSSH"):
            audit_keys(b"ssh-ed25519 not-base64")

    async def test_a_signature_naming_no_key(self) -> None:
        events = await write(FastAPIAudit([MemoryAuditSink()], signing_key=SECRET), 1)
        unknown = {**events[0].to_dict(), "signature": "rsa:abc:00"}
        assert "doesn't match its signature" in verify_audit_trail([unknown], SECRET)[0]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(audit_module, "_monotonic", clock)
    return clock


class TestCheckpoints:
    def processes(
        self,
        cache: Cache | None,
        trail: MemoryAuditSink,
        **options: Any,
    ) -> list[FastAPIAudit]:
        return [
            FastAPIAudit(
                [trail], signing_key=SECRET, checkpoint_interval=60, cache=cache, **options
            )
            for _ in range(2)
        ]

    async def test_every_stream_s_head_and_the_truncation_found(self, clock: Clock) -> None:
        trail = MemoryAuditSink()
        first, second = self.processes(Cache(InMemoryBackend()), trail)
        await write(first, 2)  # first's checkpoint after its first event
        await write(second, 2)  # second's, which sees first's head
        clock.now += 61
        (newest,) = await write(second, 1)  # second's next checkpoint publishes this head
        await write(first, 1)  # first's next one lists it
        events = list(trail.events)
        checkpoints = [event for event in events if event.action == CHECKPOINT]
        assert len(checkpoints) == 4
        heads = checkpoints[-1].details["heads"]
        assert set(heads) == {first._stream, second._stream}
        assert heads[second._stream] == [newest.sequence, newest.signature]
        assert verify_audit_trail(events, SECRET) == []
        assert last_checkpoint(events) == checkpoints[-1].time

        # second's newest events removed: first's checkpoint saw them.
        kept = [
            e for e in events if not (e.stream == second._stream and e.sequence >= newest.sequence)
        ]
        assert verify_audit_trail(kept, SECRET) == [
            f"stream {second._stream}: its events {newest.sequence} to {newest.sequence} are "
            f"missing at its end (checkpoint {checkpoints[-1].id!r})"
        ]
        # second's stream gone altogether.
        others = [e for e in events if e.stream != second._stream]
        assert any("none of its events are here" in p for p in verify_audit_trail(others, SECRET))

    async def test_an_event_replaced_by_one_signed_again(self, clock: Clock) -> None:
        trail = MemoryAuditSink()
        audit = FastAPIAudit([trail], signing_key=SECRET, checkpoint_interval=60)
        await write(audit, 2)
        clock.now += 61
        await write(audit, 1)
        events = list(trail.events)
        checkpoint = [e for e in events if e.action == CHECKPOINT][-1]
        head_sequence, _ = checkpoint.details["heads"][audit._stream]
        target = next(e for e in events if e.sequence == head_sequence)
        changed = dataclasses.replace(target, actor="mallory", signature=None)
        data = changed.to_dict()
        del data["signature"]
        resigned = dataclasses.replace(
            changed, signature=audit_keys(SECRET)[0].sign(audit_module._signed(data))
        )
        tampered = [resigned if e is target else e for e in events]
        problems = verify_audit_trail(tampered, SECRET)
        assert (
            f"stream {audit._stream}: event {head_sequence} isn't the one checkpointed"
            in " ".join(problems)
        )

    async def test_without_a_cache_its_own_stream(self) -> None:
        trail = MemoryAuditSink()
        audit = FastAPIAudit([trail], signing_key=SECRET, checkpoint_interval=60)
        await write(audit, 1)
        (checkpoint,) = [e for e in trail.events if e.action == CHECKPOINT]
        assert list(checkpoint.details["heads"]) == [audit._stream]

    async def test_a_backend_that_fails(self, caplog: pytest.LogCaptureFixture) -> None:
        class Down(InMemoryBackend):
            async def get(self, key: str) -> bytes | None:
                raise ConnectionError("down")

        trail = MemoryAuditSink()
        audit = FastAPIAudit(
            [trail], signing_key=SECRET, checkpoint_interval=60, cache=Cache(Down())
        )
        await write(audit, 1)
        (checkpoint,) = [e for e in trail.events if e.action == CHECKPOINT]
        assert list(checkpoint.details["heads"]) == [audit._stream]
        assert "without the other streams" in caplog.text

    async def test_by_hand_and_heads_of_streams_gone(self, clock: Clock) -> None:
        cache = Cache(InMemoryBackend())
        trail = MemoryAuditSink()
        audit = FastAPIAudit([trail], signing_key=SECRET, cache=cache)
        empty = await audit.checkpoint()  # before any event: no head of its own yet
        assert empty.details == {"heads": {}}
        key = cache.state_key(
            "audit", "heads:{" + hashlib.sha256(b"default").hexdigest()[:32] + "}"
        )
        await cache.backend.set(key, json.dumps({"gone": [9, "x", 0], "bad": "no"}).encode())
        await write(audit, 1)
        checkpoint = await audit.checkpoint()
        assert list(checkpoint.details["heads"]) == [audit._stream]  # the old and the bad dropped
        await cache.backend.set(key, b"not json")
        assert list((await audit.checkpoint()).details["heads"]) == [audit._stream]
        del clock

    async def test_invalid(self) -> None:
        with pytest.raises(ValueError, match="needs a signing_key"):
            FastAPIAudit(checkpoint_interval=60)
        with pytest.raises(ValueError, match="positive"):
            FastAPIAudit(signing_key=SECRET, checkpoint_interval=0)
        with pytest.raises(RuntimeError, match="need a signing_key"):
            await FastAPIAudit().checkpoint()

    def test_checkpoints_without_heads_are_ignored(self) -> None:
        assert (
            audit_module._against_checkpoints(
                {
                    "s": [
                        {
                            "sequence": 1,
                            "signature": "x",
                            "action": CHECKPOINT,
                            "details": {"heads": {"t": "?"}},
                        }
                    ]
                }
            )
            == []
        )


class TestCommandLine:
    def test_keygen_and_verify(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        private, public = tmp_path / "private.pem", tmp_path / "public.pem"
        assert main(["audit", "keygen", str(private), str(public)]) == 0
        assert stat.S_IMODE(os.stat(private).st_mode) == 0o600
        assert main(["audit", "keygen", str(private), str(public)]) == USAGE  # not overwritten
        capsys.readouterr()

        import asyncio

        trail = MemoryAuditSink()
        audit = FastAPIAudit([trail], signing_key=private.read_text(), checkpoint_interval=3600)
        asyncio.run(write(audit, 3))
        lines = [
            f"2026-11-03 INFO fastapi_alcazaba.audit.events {e.to_json()}" for e in trail.events
        ]
        log = tmp_path / "audit.log"
        log.write_text("\n".join(["a line that isn't one", "{not json", '{"other": 1}', *lines]))
        assert main(["audit", "verify", str(log), "--key", str(public)]) == 0
        out = capsys.readouterr().out
        assert "events: 4 in 1 streams" in out
        assert out.rstrip().endswith("ok")

        tampered = tmp_path / "tampered.log"
        tampered.write_text(
            "\n".join([lines[0], lines[1].replace("user-0", "mallory"), *lines[3:]])
        )
        assert main(["audit", "verify", str(tampered), "--key", str(public), "--json"]) == NOT_DONE
        report = json.loads(capsys.readouterr().out)
        assert report["events"] == 3
        assert report["problems"]

    def test_hmac_from_the_environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import asyncio

        trail = MemoryAuditSink()
        asyncio.run(write(FastAPIAudit([trail], signing_key=SECRET), 2))
        log = tmp_path / "audit.jsonl"
        log.write_text("\n".join(event.to_json() for event in trail.events))
        monkeypatch.setenv("AUDIT_KEY", SECRET)
        assert main(["audit", "verify", str(log), "--hmac-env", "AUDIT_KEY"]) == 0
        out = capsys.readouterr().out
        assert "last checkpoint: none" in out
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(log.read_text()))
        assert main(["audit", "verify", "-", "--hmac-env", "AUDIT_KEY"]) == 0

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            (["audit", "verify", "x.log"], "give the keys"),
            (["audit", "verify", "x.log", "--hmac-env", "NOT_SET_ANYWHERE"], "is empty"),
            (
                ["audit", "verify", "missing.log", "--hmac-env", "AUDIT_KEY"],
                "can't read missing.log",
            ),
            (["audit", "verify", "x.log", "--key", "missing.pem"], "can't read the key"),
            (["audit", "verify", "x.log", "--hmac-env", "SHORT_KEY"], "at least 32 bytes"),
            (["audit", "verify", "x.log", "--key", "secret.txt"], "isn't an Ed25519 key"),
        ],
    )
    def test_usage(
        self,
        argv: list[str],
        message: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "x.log").write_text("")
        (tmp_path / "secret.txt").write_text(SECRET)  # an HMAC secret where a public key goes
        monkeypatch.setenv("AUDIT_KEY", SECRET)
        monkeypatch.setenv("SHORT_KEY", "short")
        assert main(argv) == USAGE
        assert message in capsys.readouterr().err


def test_unrelated_log_records_dont_stop_the_logger(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    assert last_checkpoint([]) is None
