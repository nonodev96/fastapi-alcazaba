"""Feature flags in the cache's backend: turn features on and off for everybody, for some subjects
(users, tenants) or for a percentage of them, from any process and without a deploy.

    flags = FastAPIFeatureFlags()

    await flags.set("new-checkout", enabled=True, percentage=10, allow=["user-42"])

    if await flags.enabled("new-checkout", subject=user_id):
        ...

    @app.get("/beta/report", dependencies=[Depends(flags.require("beta-report"))])

A flag is on for a subject when it's enabled, the subject isn't in `deny`, and the subject is in
`allow` or falls within `percentage` (none: everybody, or only `allow` when it has subjects).
Rollouts are sticky: a subject's
place comes from a hash of the flag's name and the subject, so its answer stays while the
percentage does, and raising the percentage only adds subjects.

Each process keeps the definitions it reads for `refresh` seconds, so a change reaches every
process within that. When the backend fails, the last definition read is used, or the default.

Subjects are stored as SHA-256 digests salted with the flag's name, never in clear. Whoever reads
the backend can still confirm a guess of a guessable subject (an e-mail address): use opaque ids,
or give the cache an `encryption_key`, which encrypts the definitions too.
"""

import hashlib
import json
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from fastapi import HTTPException, WebSocketException
from starlette.requests import HTTPConnection
from starlette.status import HTTP_404_NOT_FOUND, WS_1008_POLICY_VIOLATION

from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable, CircuitOpen
from fastapi_alcazaba.response_cache.caches import Cache, cache_metrics, resolve_cache

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The most subjects in each of a flag's `allow` and `deny` lists.
MAX_SUBJECTS = 10_000
#: Definitions each FastAPIFeatureFlags keeps in memory (the least recently used go first).
MAX_KNOWN_FLAGS = 1_000

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DIGEST = re.compile(r"[0-9a-f]{64}")

#: Who a request is about: receives it (or the WebSocket) and returns a subject, or None.
Subject = Callable[[HTTPConnection], Awaitable[str | None] | str | None]


def _check_name(name: str) -> None:
    if not (isinstance(name, str) and _NAME.fullmatch(name)):
        raise ValueError(
            f"invalid flag name {name!r}: up to 100 letters, digits, '.', '_' and '-', "
            "starting with a letter or a digit"
        )


def _digest(name: str, subject: str) -> str:
    return hashlib.sha256(json.dumps(["subject", name, subject]).encode()).hexdigest()


def _digests(name: str, subjects: Iterable[str], what: str) -> frozenset[str]:
    if isinstance(subjects, str | bytes):
        raise TypeError(f"{what} is a list of subjects, not a single string")
    digests: set[str] = set()
    for subject in subjects:
        if not isinstance(subject, str):
            raise TypeError(f"subjects are strings, not {type(subject).__name__}")
        digests.add(_digest(name, subject))
        if len(digests) > MAX_SUBJECTS:
            raise ValueError(f"{what} can't have more than {MAX_SUBJECTS} subjects")
    return frozenset(digests)


@dataclass(frozen=True, slots=True)
class FeatureFlag:
    """A flag's definition. `allow` and `deny` hold digests of the subjects, not the subjects."""

    name: str
    enabled: bool
    percentage: float | None = None
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    @property
    def share(self) -> float:
        """The percentage of the subjects that get it: without one, everybody, or nobody but
        `allow` when there's an allow list (a flag for a few never goes to everyone by mistake)."""
        if self.percentage is not None:
            return self.percentage
        return 0.0 if self.allow else 100.0

    def is_on(self, subject: str | None = None) -> bool:
        """Whether the flag is on for `subject`. Without a subject, a partial rollout is off, and
        so is a flag with a deny list: who is denied mustn't get it by not saying who they are."""
        if not self.enabled:
            return False
        share = self.share
        if subject is None:
            return share >= 100 and not self.deny
        digest = _digest(self.name, subject)
        if digest in self.deny:
            return False
        if digest in self.allow or share >= 100:
            return True
        place = hashlib.sha256(json.dumps(["rollout", self.name, subject]).encode()).digest()
        # Compared as integers: a float of the place could round up to 100.
        return int.from_bytes(place[:8], "big") * 100 < share * 2**64


def _subject_digests(value: Any) -> frozenset[str] | None:
    if not isinstance(value, list) or len(value) > MAX_SUBJECTS:
        return None
    if not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in value):
        return None
    return frozenset(value)


def _parse(name: str, payload: bytes) -> FeatureFlag | None:
    """The definition stored by `set`; None when it isn't one."""
    try:
        data: Any = json.loads(payload)
        enabled, percentage = data["enabled"], data["percentage"]
        allow, deny = _subject_digests(data["allow"]), _subject_digests(data["deny"])
    except (TypeError, ValueError, KeyError):
        return None
    if not isinstance(enabled, bool) or allow is None or deny is None:
        return None
    if percentage is not None and not (type(percentage) in (int, float) and 0 <= percentage <= 100):
        return None
    return FeatureFlag(
        name, enabled, None if percentage is None else float(percentage), allow, deny
    )


class FastAPIFeatureFlags:
    """Feature flags shared by every process through the cache's backend.

    :param cache: the Cache whose backend keeps the flags; FastAPICache's by default, resolved at
        each call.
    :param refresh: seconds each process keeps a definition before asking the backend again.
    :param namespace: part of the keys, after the cache's prefix (see `Cache.state_key`).
    """

    def __init__(
        self,
        cache: Cache | None = None,
        refresh: float = 5.0,
        *,
        namespace: str = "flags",
    ) -> None:
        if not (math.isfinite(refresh) and refresh > 0):
            raise ValueError("refresh must be a positive number of seconds")
        self.cache = cache
        self.refresh = refresh
        self.namespace = namespace
        # Flag name -> (when it was read, in monotonic time; its definition, None if it has none).
        self._known: OrderedDict[str, tuple[float, FeatureFlag | None]] = OrderedDict()
        # Moves on with every change made here: a read that started before one is stale.
        self._epoch = 0
        self._source: Cache | None = None  # the cache the copies come from

    def _key(self, cache: Cache, name: str) -> str:
        return cache.state_key(self.namespace, name)

    def forget(self) -> None:
        """Drop this process's copies of the definitions: the next evaluations read them from the
        backend."""
        self._known.clear()
        self._epoch += 1

    def _cache(self) -> Cache:
        """The cache; copies read from another one (the app's set up again, as each test's
        lifespan does) are dropped."""
        cache = resolve_cache(self.cache)
        if cache is not self._source:
            self._source = cache
            self.forget()
        return cache

    async def set(
        self,
        name: str,
        *,
        enabled: bool,
        percentage: float | None = None,
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
    ) -> FeatureFlag:
        """Define the flag `name` (replacing its definition) for every process, within `refresh`
        seconds. `percentage` (0 to 100) of the subjects get it, those in `allow` always do and
        those in `deny` never. Without a percentage everybody does, or only `allow` when given.
        Raises BackendUnavailable when the backend fails."""
        _check_name(name)
        if not isinstance(enabled, bool):  # a string such as "false" would be true
            raise TypeError("enabled must be True or False")
        if percentage is not None and not (math.isfinite(percentage) and 0 <= percentage <= 100):
            raise ValueError("percentage must be between 0 and 100")
        flag = FeatureFlag(
            name,
            enabled,
            None if percentage is None else float(percentage),
            _digests(name, allow, "allow"),
            _digests(name, deny, "deny"),
        )
        payload = json.dumps(
            {
                "enabled": flag.enabled,
                "percentage": flag.percentage,
                "allow": sorted(flag.allow),
                "deny": sorted(flag.deny),
            }
        ).encode()
        cache = self._cache()
        key = self._key(cache, name)
        protector = cache.options.protector
        value = protector.seal(key, payload) if protector is not None else payload
        backend = shared_backend(cache.backend)
        await cache.guard.run("set", lambda: backend.set(key, value))
        self._epoch += 1
        self._store(name, flag)
        return flag

    async def delete(self, name: str) -> None:
        """Remove the flag `name`: `enabled` gives the default again. Raises BackendUnavailable
        when the backend fails."""
        _check_name(name)
        cache = self._cache()
        key, backend = self._key(cache, name), shared_backend(cache.backend)
        await cache.guard.run("delete", lambda: backend.clear(key=key))
        self._epoch += 1
        self._store(name, None)

    async def get(self, name: str) -> FeatureFlag | None:
        """The definition of `name` in the backend now, None if it has none; this process's copy
        is updated with it. Raises BackendUnavailable when the backend fails."""
        _check_name(name)
        cache = self._cache()
        epoch = self._epoch
        flag = await self._read(cache, name)
        if epoch == self._epoch:
            self._store(name, flag)
        return flag

    async def enabled(self, name: str, subject: str | None = None, default: bool = False) -> bool:
        """Whether the flag `name` is on for `subject`; `default` when it isn't defined, or the
        backend fails and this process never read it."""
        _check_name(name)
        flag = await self._definition(name)
        result = default if flag is None else flag.is_on(subject)
        outcome = "default" if flag is None else "on" if result else "off"
        cache_metrics(self.cache).count("flags.evaluations", flag=name, result=outcome)
        return result

    def require(
        self,
        name: str,
        subject: Subject | None = None,
        *,
        default: bool = False,
    ) -> Callable[[HTTPConnection], Awaitable[None]]:
        """A FastAPI dependency that answers 404 (FastAPI's own "Not Found", so the flag isn't
        revealed) when the flag `name` is off for the request's `subject`, and closes WebSockets
        with 1008. For subjects that need other dependencies (the current user), call `enabled`
        from your own dependency instead."""
        _check_name(name)

        async def require_flag(connection: HTTPConnection) -> None:
            who: str | None = None
            if subject is not None:
                found = subject(connection)
                who = await found if isawaitable(found) else found
            if not await self.enabled(name, who, default):
                if connection.scope["type"] == "websocket":
                    raise WebSocketException(WS_1008_POLICY_VIOLATION)
                raise HTTPException(HTTP_404_NOT_FOUND)

        return require_flag

    async def _definition(self, name: str) -> FeatureFlag | None:
        self._cache()  # copies from another cache are dropped first
        known = self._known.get(name)
        now = time.monotonic()
        if known is not None:
            read_at, flag = known
            if now - read_at < self.refresh:
                self._known.move_to_end(name)
                return flag
            self._known[name] = (now, flag)  # the other calls use it while this one asks
        try:
            return await self.get(name)
        except BackendUnavailable as exc:
            if isinstance(exc, CircuitOpen):
                logger.debug("Feature flag %r not refreshed: %s", name, exc)
            else:
                logger.warning(
                    "Feature flag %r not refreshed: the backend failed", name, exc_info=exc
                )
            last = known[1] if known is not None else None
            self._store(name, last)  # not asked again before `refresh`
            return last

    def _store(self, name: str, flag: FeatureFlag | None) -> None:
        self._known[name] = (time.monotonic(), flag)
        self._known.move_to_end(name)
        if len(self._known) > MAX_KNOWN_FLAGS:
            self._known.popitem(last=False)

    async def _read(self, cache: Cache, name: str) -> FeatureFlag | None:
        key, backend = self._key(cache, name), shared_backend(cache.backend)
        data = await cache.guard.run("get", lambda: backend.get(key))
        if data is None:
            return None
        protector = cache.options.protector
        if protector is not None and (data := protector.open(key, data)) is None:
            logger.warning(
                "Ignored a definition of the feature flag %r that this app didn't write", name
            )
            return None
        flag = _parse(name, data)
        if flag is None:
            logger.warning("Ignored a malformed definition of the feature flag %r", name)
        return flag
