"""A task queue on Redis or Valkey streams: work that an endpoint hands over to run in the
background, in this process or in any other that shares the cache's backend.

    queue = FastAPITaskQueue()

    @queue.task(retries=5)
    async def send_welcome_email(user_id: int) -> None: ...

    app = FastAPI(lifespan=queue.lifespan)  # this process runs tasks too

    @app.post("/users")
    async def create_user(user: NewUser) -> None:
        ...
        await queue.enqueue(send_welcome_email, user.id)

Delivery is at least once: a task leaves the queue only once it has finished, so a process that
dies while running one (or can't tell the backend it finished) means that it runs again
elsewhere. Write tasks so that running one twice does no harm (idempotent), e.g. by checking
first whether the email was already sent.

A task that raises is retried with exponential backoff and jitter; after its last retry it goes
to the dead letters with the type of its error (not the message, which may hold personal data),
where `dead_letters()` lists them and `requeue_dead_letters()` puts them back.

Only registered tasks run, with arguments that travel as JSON (never pickle). With a
`signing_key` or `encryption_key` on the cache every task is signed or encrypted and bound to its
queue: a task that someone planted in the backend or altered is refused, logged and sent to the
dead letters, never run. Each delivery carries a run id that only one entry may claim, and a
deadline (`replay_window` after it was due): a copy of a sealed task written back to the queue
is refused as a replay, and one older than its deadline as stale. Without a key, whoever can
write to the backend can run the registered tasks with arguments of their choosing.

With tracing on, each run is the root of a trace of its own, linked to the span that enqueued the
task (the request's, with FastAPI's instrumentation).
"""

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, TypeVar

from fastapi_alcazaba.backends._redis_common import RedisLikeBackend
from fastapi_alcazaba.backends.tiered import TieredBackend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache
from fastapi_alcazaba.telemetry.tracing import record_failure, traceparent

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

F = TypeVar("F", bound=Callable[..., Any])

_GROUP = "workers"
_FORMAT = 2
# Room for the signature or the encryption header and tag around a payload (67 bytes at most),
# and for the attempt counter of a retry growing by a digit or two.
_SEAL_OVERHEAD = 128
_PROMOTE_BATCH = 100
_PAGE = 100
_MAX_PAUSE = 30.0
_STREAM_ID = re.compile(r"[0-9]+-[0-9]+")
_RUN_ID = re.compile(r"[0-9a-f]{32}")

_REASONS = {
    "malformed": "its payload isn't a task",
    "tampered": "its signature doesn't match (written without the key, or altered)",
    "too_large": "its payload is larger than max_payload_size",
    "unknown_task": "no task of this process has its name",
    "invalid_arguments": "its arguments don't fit the task's signature",
    "abandoned": "its workers stopped answering while running it, too many times",
    "stale": "it waited in the queue longer than replay_window",
    "replayed": "another entry already claimed its run: a copy written back to the queue",
}

# Keys and payloads always travel as KEYS/ARGV, never inside the script source. Every key of a
# queue carries the same {hash tag}, so a cluster keeps them in one slot. Reads go through
# scripts too: their replies have the same shape whatever protocol the client speaks.

# The payload field of an entry (a flat list of fields and values); "" when there is none.
_PAYLOAD_LUA = """
local function payload(fields)
    if type(fields) ~= 'table' then
        return ''
    end
    for i = 1, #fields, 2 do
        if fields[i] == 'p' then
            return fields[i + 1]
        end
    end
    return ''
end
"""

# KEYS[1]: stream; ARGV[1]: group; ARGV[2]: consumer; ARGV[3]: how many. {id, payload, 1} each.
READ_SCRIPT = (
    _PAYLOAD_LUA
    + """
local reply = redis.call('XREADGROUP', 'GROUP', ARGV[1], ARGV[2], 'COUNT', ARGV[3],
                         'STREAMS', KEYS[1], '>')
local entries = {}
if type(reply) == 'table' and reply[1] then
    -- {{stream, entries}}; {stream, entries} from servers that flatten it (fakeredis).
    local found = type(reply[1]) == 'table' and reply[1] or reply
    for _, entry in ipairs(found[2]) do
        entries[#entries + 1] = {entry[1], payload(entry[2]), 1}
    end
end
return entries
"""
)

# Takes over the entries that have been pending for at least ARGV[3] milliseconds (their worker
# died), in one step so that two workers never both take one. KEYS[1]: stream; ARGV[1]: group;
# ARGV[2]: consumer; ARGV[4]: how many. {id, payload, deliveries} each.
RECLAIM_SCRIPT = (
    _PAYLOAD_LUA
    + """
local entries = {}
for _, stale in ipairs(redis.call('XPENDING', KEYS[1], ARGV[1], 'IDLE', ARGV[3], '-', '+',
                                  ARGV[4])) do
    local claimed = redis.call('XCLAIM', KEYS[1], ARGV[1], ARGV[2], ARGV[3], stale[1])
    if claimed[1] then
        entries[#entries + 1] = {stale[1], payload(claimed[1][2]), stale[4] + 1}
    else
        -- Deleted from the stream: off the pending list too (Redis 6.2 keeps it there).
        redis.call('XACK', KEYS[1], ARGV[1], stale[1])
    end
end
return entries
"""
)

# KEYS[1]: stream; KEYS[2]: delayed tasks; ARGV[1]: most tasks the queue holds; ARGV[2]: payload;
# ARGV[3]: when it's due (Unix time), "" for now. 0 when the queue is full.
ENQUEUE_SCRIPT = """
if redis.call('XLEN', KEYS[1]) + redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[1]) then
    return 0
end
if ARGV[3] == '' then
    redis.call('XADD', KEYS[1], '*', 'p', ARGV[2])
else
    redis.call('ZADD', KEYS[2], ARGV[3], ARGV[2])
end
return 1
"""

# Moves the delayed tasks that are due to the stream, at most ARGV[2] of them, in one step: two
# processes never both move one. KEYS[1]: delayed tasks; KEYS[2]: stream; ARGV[1]: now.
PROMOTE_SCRIPT = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
for _, payload in ipairs(due) do
    redis.call('XADD', KEYS[2], '*', 'p', payload)
    redis.call('ZREM', KEYS[1], payload)
end
return #due
"""

# A task finished: off the pending list and out of the stream. KEYS[1]: stream; ARGV[1]: group;
# ARGV[2]: entry.
ACK_SCRIPT = """
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return redis.call('XDEL', KEYS[1], ARGV[2])
"""

# Whether the run KEYS[1] is the entry ARGV[1]'s: the first entry to ask claims it for ARGV[2]
# ms; the same entry, taken over by another worker, still may; a copy may not.
CLAIM_SCRIPT = """
local holder = redis.call('GET', KEYS[1])
if holder == false then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
    return 1
end
return holder == ARGV[1] and 1 or 0
"""

# A task to run again at ARGV[3] (a retry, or one cut short by a shutdown), unless another
# worker settled it already. KEYS[1]: stream; KEYS[2]: delayed tasks; ARGV[1]: group; ARGV[2]:
# entry; ARGV[4]: payload.
RETRY_SCRIPT = """
if redis.call('XACK', KEYS[1], ARGV[1], ARGV[2]) == 0 then
    return 0
end
redis.call('XDEL', KEYS[1], ARGV[2])
redis.call('ZADD', KEYS[2], ARGV[3], ARGV[4])
return 1
"""

# A task to the dead letters (at most about ARGV[3] of them are kept), unless another worker
# settled it already. KEYS[1]: stream; KEYS[2]: dead letters; ARGV[1]: group; ARGV[2]: entry;
# ARGV[4..10]: payload, reason, error type, task, task id, attempts, time.
BURY_SCRIPT = """
if redis.call('XACK', KEYS[1], ARGV[1], ARGV[2]) == 0 then
    return 0
end
redis.call('XDEL', KEYS[1], ARGV[2])
redis.call('XADD', KEYS[2], 'MAXLEN', '~', ARGV[3], '*', 'p', ARGV[4], 'reason', ARGV[5],
           'error', ARGV[6], 'task', ARGV[7], 'task_id', ARGV[8], 'attempts', ARGV[9],
           'at', ARGV[10])
return 1
"""

# A dead letter back in the queue, once. KEYS[1]: dead letters; KEYS[2]: stream; KEYS[3]: delayed
# tasks; ARGV[1]: dead letter; ARGV[2]: most tasks the queue holds; ARGV[3]: new payload. -1 when
# the queue is full, 0 when the dead letter is gone.
REQUEUE_SCRIPT = """
if redis.call('XLEN', KEYS[2]) + redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[2]) then
    return -1
end
if redis.call('XDEL', KEYS[1], ARGV[1]) == 0 then
    return 0
end
redis.call('XADD', KEYS[2], '*', 'p', ARGV[3])
return 1
"""

# KEYS[1]: stream; KEYS[2]: delayed tasks.
PENDING_SCRIPT = "return redis.call('XLEN', KEYS[1]) + redis.call('ZCARD', KEYS[2])"

# A stopping worker leaves the group, unless it still holds tasks (others reclaim them later).
# KEYS[1]: stream; ARGV[1]: group; ARGV[2]: consumer.
LEAVE_SCRIPT = """
if #redis.call('XPENDING', KEYS[1], ARGV[1], '-', '+', 1, ARGV[2]) > 0 then
    return 0
end
redis.call('XGROUP', 'DELCONSUMER', KEYS[1], ARGV[1], ARGV[2])
return 1
"""


class QueueFull(Exception):
    """The queue already holds `max_length` tasks: the workers aren't keeping up."""


@dataclass(frozen=True)
class DeadLetter:
    """A task that won't run again unless it's requeued. Its arguments aren't shown."""

    #: Its ID in the dead letters, for `requeue_dead_letters` and `discard_dead_letters`.
    id: str
    #: Why it's here: failed, unknown_task, invalid_arguments, abandoned, malformed, tampered or
    #: too_large.
    reason: str
    #: The task's name and ID, None when its payload couldn't be read.
    task: str | None
    task_id: str | None
    #: The type of the exception its last attempt raised, for `failed`.
    error: str | None
    attempts: int
    failed_at: datetime


@dataclass(frozen=True)
class _Task:
    name: str
    func: Callable[..., Any]
    signature: inspect.Signature
    retries: int
    backoff: float
    max_backoff: float

    def delay(self, failures: int) -> float:
        """Seconds before the retry after the `failures`-th failure: doubling each time up to
        `max_backoff`, then between half and all of that, so that the retries of tasks that
        failed together (a service down) spread out."""
        ceiling = min(self.backoff * 2.0 ** min(failures - 1, 64), self.max_backoff)
        return ceiling * random.uniform(0.5, 1.0)  # noqa: S311  # jitter, not a secret


@dataclass(frozen=True)
class _Entry:
    id: str
    payload: bytes
    deliveries: int


def _is_async(func: Callable[..., Any]) -> bool:
    # A function, or an object with an async __call__.
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(type(func).__call__)


def _is_task(body: Any) -> bool:
    return (
        isinstance(body, dict)
        and body.get("v") == _FORMAT
        and isinstance(body.get("id"), str)
        and isinstance(body.get("task"), str)
        and isinstance(body.get("args"), list)
        and isinstance(body.get("kwargs"), dict)
        and type(body.get("attempt")) is int
        and body["attempt"] >= 0
        and isinstance(body.get("trace", ""), str)
        and isinstance(body.get("run"), str)
        and _RUN_ID.fullmatch(body["run"]) is not None
        and type(body.get("until")) in (int, float)
    )


def _fits(task: _Task, body: dict[str, Any]) -> bool:
    try:
        task.signature.bind(*body["args"], **body["kwargs"])
    except TypeError:
        return False
    return True


def _error_type(exc: BaseException) -> str:
    cls = type(exc)
    return (
        cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"
    )


def _text(value: bytes | str) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _stream_ids(ids: Iterable[str]) -> list[str]:
    found = list(ids)
    for dead_id in found:
        if not _STREAM_ID.fullmatch(dead_id):
            raise ValueError(f"{dead_id!r} isn't the ID of a dead letter")
    return found


def _dead_letter(entry_id: bytes | str, fields: dict[bytes, bytes]) -> DeadLetter:
    """A dead letter as the backend has it. Anyone who can write to it can add entries, so
    nothing in them is trusted to be well formed."""

    def text(name: str) -> str:
        return _text(fields.get(name.encode(), b""))

    attempts, at = text("attempts"), text("at")
    try:
        failed_at = datetime.fromtimestamp(float(at), UTC)
    except (ValueError, OverflowError, OSError):
        failed_at = datetime.fromtimestamp(0, UTC)
    return DeadLetter(
        id=_text(entry_id),
        reason=text("reason"),
        task=text("task") or None,
        task_id=text("task_id") or None,
        error=text("error") or None,
        attempts=int(attempts) if attempts.isascii() and attempts.isdigit() else 0,
        failed_at=failed_at,
    )


class FastAPITaskQueue:
    """Tasks that run in the background, in any process sharing the cache's backend.

    Every process enqueues; those that call `start()` (or use `lifespan` or `async with`) also
    run tasks. A worker-only process: `async with queue: await asyncio.Event().wait()`.

    Needs Redis 6.2+ or Valkey through RedisBackend or ValkeyBackend (or a TieredBackend in front
    of one). Its keys are state keys (`Cache.state_key`), which `clear()` never deletes.

    :param cache: whose backend holds the queue; FastAPICache's by default, resolved when first
        needed.
    :param name: queues with different names are independent.
    :param concurrency: tasks run at a time by `start()` in this process.
    :param grace: seconds `stop()` waits for the tasks that are running; those still running then
        are cancelled and go back to the queue (a sync task's thread can't be stopped: it runs
        to its end, so that task may run twice).
    :param poll_interval: seconds between looks at the backend when there's nothing to do. A
        task enqueued in this process wakes its workers at once.
    :param visibility_timeout: seconds after which the tasks of a worker that stopped answering
        (its process died) are taken over by another. Running tasks are kept from it every third
        of it, however long they run.
    :param max_deliveries: times a task is handed to a worker (once, plus each time it's taken
        over from a worker that died) before it goes to the dead letters instead: it may be what
        kills them.
    :param max_payload_size: largest arguments, in bytes of JSON; anything larger is refused.
    :param max_length: most tasks the queue holds (waiting, running or delayed); `enqueue` raises
        QueueFull beyond, instead of dropping tasks.
    :param max_dead_letters: dead letters kept, about: the oldest go first.
    :param replay_window: seconds a task may wait in the queue after it's due; later, it's
        refused as stale and goes to the dead letters (`requeue_dead_letters()` sends it again).
        Its run id is kept that long, so that a copy of it is refused.
    """

    def __init__(
        self,
        cache: Cache | None = None,
        *,
        name: str = "default",
        concurrency: int = 10,
        grace: float = 10.0,
        poll_interval: float = 1.0,
        visibility_timeout: float = 60.0,
        max_deliveries: int = 5,
        max_payload_size: int = 65_536,
        max_length: int = 100_000,
        max_dead_letters: int = 10_000,
        replay_window: float = 7 * 24 * 3600,
    ) -> None:
        if not name:
            raise ValueError("the queue needs a name")
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if grace < 0:
            raise ValueError("grace can't be negative")
        if poll_interval <= 0 or visibility_timeout <= 0 or replay_window <= 0:
            raise ValueError("poll_interval, visibility_timeout and replay_window must be positive")
        if min(max_deliveries, max_payload_size, max_length, max_dead_letters) < 1:
            raise ValueError(
                "max_deliveries, max_payload_size, max_length and max_dead_letters must be at "
                "least 1"
            )
        self.cache = cache
        self.name = name
        self.concurrency = concurrency
        self.grace = grace
        self.poll_interval = poll_interval
        self.visibility_timeout = visibility_timeout
        self.max_deliveries = max_deliveries
        self.max_payload_size = max_payload_size
        self.max_length = max_length
        self.max_dead_letters = max_dead_letters
        self.replay_window = replay_window
        self._tasks: dict[str, _Task] = {}
        self._names: dict[Callable[..., Any], str] = {}
        self._resolved: Cache | None = None
        self._client: Any = None
        self._stream = self._delayed = self._dead = self._runs = self._seal_key = ""
        self._started = False
        self._stopping = False
        self._exiting = False  # the worker's own loop ends: stop() is done waiting for tasks
        self._consumer = ""
        self._workers = concurrency
        self._running: dict[str, asyncio.Task[None]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._wakeup = asyncio.Event()
        self._drained = asyncio.Event()

    # region Tasks

    def task(
        self,
        name: str | None = None,
        *,
        retries: int = 3,
        backoff: float = 1.0,
        max_backoff: float = 600.0,
    ) -> Callable[[F], F]:
        """Register the decorated function (async, or sync run in a thread) as a task. What it
        returns is ignored: keep results elsewhere.

        :param name: the task's name in the queue, which travels with every task (keep it when
            the function moves); the function's module and name by default.
        :param retries: times a task that raised is tried again: `backoff` seconds after the
            first failure, twice that after the second... up to `max_backoff`, each with jitter.
            Then it goes to the dead letters. 0: at the first failure.
        """
        if retries < 0:
            raise ValueError("retries can't be negative")
        if backoff < 0 or max_backoff < 0:
            raise ValueError("backoff and max_backoff can't be negative")

        def register(func: F) -> F:
            task_name = name if name is not None else f"{func.__module__}.{func.__qualname__}"
            if task_name in self._tasks:
                raise ValueError(f"there's already a task named {task_name!r}")
            signature = inspect.signature(func)
            self._tasks[task_name] = _Task(
                task_name, func, signature, retries, backoff, max_backoff
            )
            self._names[func] = task_name
            return func

        return register

    async def enqueue(
        self,
        task: Callable[..., Any] | str,
        /,
        *args: Any,
        delay: float | timedelta | None = None,
        **kwargs: Any,
    ) -> str:
        """Queue a run of `task` (a registered function, or its name) with these arguments, to
        start in `delay` seconds or as soon as a worker is free; returns the task's ID.

        The arguments must fit the function and be JSON-serialisable (checked here, raising
        TypeError); they arrive as JSON gives them back: tuples as lists, dict keys as strings.
        A task can't take an argument named `delay` by keyword. Raises QueueFull when the queue
        holds `max_length` tasks, and BackendUnavailable when the backend fails."""
        definition = self._lookup(task)
        try:
            definition.signature.bind(*args, **kwargs)
        except TypeError as exc:
            raise TypeError(f"{definition.name}: {exc}") from None
        seconds = delay.total_seconds() if isinstance(delay, timedelta) else (delay or 0.0)
        if seconds < 0:
            raise ValueError("delay can't be negative")
        cache = self._ready()
        task_id = uuid.uuid4().hex
        body = {
            "v": _FORMAT,
            "id": task_id,
            "task": definition.name,
            "args": list(args),
            "kwargs": kwargs,
            "attempt": 0,
            **self._delivery(seconds),
        }
        if cache.tracer.enabled and (origin := traceparent()):
            body["trace"] = origin
        raw = self._dump(body)
        if len(raw) > self.max_payload_size:
            raise ValueError(
                f"the arguments of {definition.name} take {len(raw)} bytes as JSON, more than "
                f"max_payload_size ({self.max_payload_size})"
            )
        due = repr(time.time() + seconds) if seconds > 0 else ""
        keys = [self._stream, self._delayed]
        payload = self._seal(raw)
        added = await self._eval(
            "task_enqueue", ENQUEUE_SCRIPT, keys, str(self.max_length), payload, due
        )
        if not added:
            raise QueueFull(f"the task queue {self.name!r} already holds {self.max_length} tasks")
        cache.metrics.count("tasks.enqueued", queue=self.name, task=definition.name)
        if not due:
            self._wakeup.set()
        return task_id

    def _lookup(self, task: Callable[..., Any] | str) -> _Task:
        name = task if isinstance(task, str) else self._names.get(task)
        if name is None or name not in self._tasks:
            raise ValueError(
                f"{task!r} isn't a task of the queue {self.name!r}: register it with @queue.task()"
            )
        return self._tasks[name]

    def _delivery(self, delay: float) -> dict[str, Any]:
        """A fresh run id and deadline, for each time a task goes (back) to the queue."""
        return {"run": uuid.uuid4().hex, "until": time.time() + delay + self.replay_window}

    def _dump(self, body: dict[str, Any]) -> bytes:
        try:
            return json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"the arguments of {body['task']} must be JSON-serialisable: {exc}"
            ) from None

    def _seal(self, raw: bytes) -> bytes:
        assert self._resolved is not None  # noqa: S101  # resolved by _ready()
        protector = self._resolved.options.protector
        return protector.seal(self._seal_key, raw) if protector is not None else raw

    def _open(self, data: bytes) -> dict[str, Any] | str:
        """The task in `data`, or the reason why it's refused."""
        assert self._resolved is not None  # noqa: S101  # resolved by _ready()
        if len(data) > self.max_payload_size + _SEAL_OVERHEAD:
            return "too_large"
        protector = self._resolved.options.protector
        raw = protector.open(self._seal_key, data) if protector is not None else data
        if raw is None:
            return "tampered"
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: deeply nested
            return "malformed"
        return body if _is_task(body) else "malformed"

    # endregion

    # region Backend

    def _ready(self) -> Cache:
        """The cache, checked when it changes: its backend must hold Redis streams. A running
        queue keeps the one it started with; a stopped one follows FastAPICache, which the app's
        next lifespan (a test's) sets up again."""
        if self._resolved is not None and (self._started or self._resolved is self._resolve()):
            return self._resolved
        cache = self._resolve()
        backend = cache.backend
        if isinstance(backend, TieredBackend):
            backend = backend.remote
        if not isinstance(backend, RedisLikeBackend):
            raise NotImplementedError(
                f"{type(backend).__name__} can't hold a task queue: it needs Redis streams "
                "(RedisBackend or ValkeyBackend, or a TieredBackend in front of one)"
            )
        digest = hashlib.sha256(self.name.encode()).hexdigest()[:32]
        base = cache.state_key("tasks", f"{{{digest}}}")
        self._stream, self._delayed, self._dead, self._runs = (
            f"{base}:stream",
            f"{base}:delayed",
            f"{base}:dead",
            f"{base}:run:",
        )
        # What signatures bind tasks to: a task sealed for one queue doesn't open in another.
        self._seal_key = f"{base}:task"
        self._client = backend.client
        self._resolved = cache
        return cache

    def _resolve(self) -> Cache:
        if self.cache is not None:
            return self.cache
        from fastapi_alcazaba.response_cache.caches import FastAPICache

        return FastAPICache.instance()

    async def _eval(self, operation: str, script: str, keys: list[str], *args: str | bytes) -> Any:
        assert self._resolved is not None  # noqa: S101  # resolved by _ready()
        client = self._client
        return await self._resolved.guard.run(
            operation, lambda: client.eval(script, len(keys), *keys, *args)
        )

    async def _call(self, operation: str, command: str, *args: Any, **kwargs: Any) -> Any:
        assert self._resolved is not None  # noqa: S101  # resolved by _ready()
        method = getattr(self._client, command)
        return await self._resolved.guard.run(operation, lambda: method(*args, **kwargs))

    # endregion

    # region Workers

    async def start(self, concurrency: int | None = None) -> None:
        """Start running tasks, up to `concurrency` at a time (the queue's by default)."""
        if self._started:
            raise RuntimeError("the task queue is already running")
        workers = self.concurrency if concurrency is None else concurrency
        if workers < 1:
            raise ValueError("concurrency must be at least 1")
        cache = self._ready()
        if cache.options.protector is None:
            logger.warning(
                "The tasks of the queue %r aren't signed: whoever can write to the backend can "
                "run them with arguments of their choosing. Give the cache a signing_key or an "
                "encryption_key",
                self.name,
            )
        self._workers = workers
        self._consumer = f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
        self._started, self._stopping, self._exiting = True, False, False
        # New ones each time: an Event belongs to the event loop that first waits on it, and the
        # next start may be in another (a test's).
        self._drained, self._wakeup = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        self._worker = loop.create_task(self._work(), name=f"task queue {self.name}")

    async def stop(self) -> None:
        """Stop: no more tasks are taken, those running get `grace` seconds to finish, and those
        still running then are cancelled and go back to the queue."""
        if not self._started:
            return
        assert self._worker is not None  # noqa: S101  # set by start()
        self._stopping = True
        self._wakeup.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.grace
        try:
            async with asyncio.timeout_at(deadline):
                await self._drained.wait()
        except TimeoutError:
            # Stuck in a backend call: cancelled, so that it can't start more tasks.
            self._worker.cancel()
        handlers = list(self._running.values())
        if handlers:
            _, late = await asyncio.wait(handlers, timeout=max(deadline - loop.time(), 0))
            if late:
                logger.warning(
                    "Cancelling %d tasks of the queue %r still running: back to the queue",
                    len(late),
                    self.name,
                )
                for handler in late:
                    handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
        # Told as well as cancelled: a client that swallows the cancellation (fakeredis does on
        # Python 3.11) would otherwise keep the worker going, and stop() waiting for it.
        self._exiting = True
        self._wakeup.set()
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        with contextlib.suppress(BackendUnavailable):
            await self._eval("task_leave", LEAVE_SCRIPT, [self._stream], _GROUP, self._consumer)
        self._started = False

    @asynccontextmanager
    async def lifespan(self, app: Any = None) -> AsyncIterator[None]:
        """For `FastAPI(lifespan=queue.lifespan)`: runs tasks while the app runs."""
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    async def __aenter__(self) -> "FastAPITaskQueue":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def _work(self) -> None:
        """Takes tasks while there's room, and looks after the queue: delayed tasks that are
        due, tasks of dead workers, heartbeats of the running ones."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        next_heartbeat, next_promotion, next_reclaim = now + self.visibility_timeout / 3, now, now
        group_ready, failures = False, 0
        while not self._exiting:
            if self._stopping:
                self._drained.set()  # nothing more is read: the running tasks are all there is
            now = loop.time()
            pause = self.poll_interval
            try:
                if not group_ready:
                    await self._create_group()
                    group_ready = True
                if now >= next_heartbeat:
                    next_heartbeat = now + self.visibility_timeout / 3
                    await self._heartbeat()
                if now >= next_promotion:
                    next_promotion = now + self.poll_interval
                    if await self._promote() == _PROMOTE_BATCH:
                        next_promotion, pause = now, 0.0  # more may be due
                free = self._workers - len(self._running)
                if free > 0 and not self._stopping:
                    entries: list[_Entry] = []
                    if now >= next_reclaim:
                        next_reclaim = now + self.visibility_timeout / 2
                        entries = await self._reclaim(free)
                    if not entries:
                        entries = await self._read(free)
                    for entry in entries:
                        self._spawn(entry)
                    if entries:
                        pause = 0.0
                failures = 0
            except Exception as exc:
                # The group goes with the stream if its key is deleted: create it again.
                group_ready = False
                failures += 1
                pause = min(self.poll_interval * 2.0 ** min(failures, 16), _MAX_PAUSE)
                logger.warning(
                    "The task queue %r failed to reach the backend: trying again in %.1f s",
                    self.name,
                    pause,
                    exc_info=exc,
                )
            if pause > 0:
                await self._sleep(pause)

    async def _sleep(self, seconds: float) -> None:
        """Wait `seconds`, or less when woken: a task enqueued here, a task done, `stop()`."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wakeup.wait()
        self._wakeup.clear()

    async def _create_group(self) -> None:
        stream, client = self._stream, self._client

        async def create() -> None:
            try:
                # From the start: tasks enqueued before any worker ever ran are delivered too.
                await client.xgroup_create(stream, _GROUP, id="0", mkstream=True)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

        assert self._resolved is not None  # noqa: S101  # resolved by start()
        await self._resolved.guard.run("task_group", create)

    async def _heartbeat(self) -> None:
        """Claim the running tasks again, which resets their idle time: however long they run,
        no other worker takes them over while this process is alive."""
        if self._running:
            ids = list(self._running)
            await self._call(
                "task_heartbeat",
                "xclaim",
                self._stream,
                _GROUP,
                self._consumer,
                0,
                ids,
                justid=True,
            )

    async def _promote(self) -> int:
        keys = [self._delayed, self._stream]
        moved = await self._eval(
            "task_promote", PROMOTE_SCRIPT, keys, repr(time.time()), str(_PROMOTE_BATCH)
        )
        return int(moved)

    async def _read(self, count: int) -> list[_Entry]:
        rows = await self._eval(
            "task_read", READ_SCRIPT, [self._stream], _GROUP, self._consumer, str(count)
        )
        return [_Entry(_text(row[0]), row[1], int(row[2])) for row in rows]

    async def _reclaim(self, count: int) -> list[_Entry]:
        idle = str(max(1, round(self.visibility_timeout * 1000)))
        rows = await self._eval(
            "task_reclaim", RECLAIM_SCRIPT, [self._stream], _GROUP, self._consumer, idle, str(count)
        )
        entries = [_Entry(_text(row[0]), row[1], int(row[2])) for row in rows]
        assert self._resolved is not None  # noqa: S101  # resolved by start()
        for _ in entries:
            self._resolved.metrics.count("tasks.reclaimed", queue=self.name)
        if entries:
            logger.info(
                "Took over %d tasks of the queue %r from workers that stopped answering",
                len(entries),
                self.name,
            )
        return entries

    def _spawn(self, entry: _Entry) -> None:
        if entry.id in self._running:
            return  # taken over while it still runs here: its heartbeats didn't get through
        loop = asyncio.get_running_loop()
        handler = loop.create_task(self._handle(entry), name=f"task of the queue {self.name}")
        self._running[entry.id] = handler

    async def _handle(self, entry: _Entry) -> None:
        try:
            await self._process(entry)
        except Exception:
            logger.warning(
                "Couldn't settle a task of the queue %r: it runs again after the visibility "
                "timeout",
                self.name,
                exc_info=True,
            )
        finally:
            del self._running[entry.id]
            self._wakeup.set()

    async def _process(self, entry: _Entry) -> None:
        body = self._open(entry.payload)
        if isinstance(body, str):
            # Too large: not copied to the dead letters, it couldn't be requeued anyway.
            await self._refuse(entry, body, b"" if body == "too_large" else entry.payload)
            return
        if body["until"] < time.time():
            await self._refuse(entry, "stale", entry.payload, body)
            return
        if not await self._claim(entry, body):
            await self._refuse(entry, "replayed", entry.payload, body)
            return
        task = self._tasks.get(body["task"])
        if task is None:
            await self._refuse(entry, "unknown_task", entry.payload, body)
        elif entry.deliveries > self.max_deliveries:
            await self._refuse(entry, "abandoned", entry.payload, body)
        elif not _fits(task, body):
            await self._refuse(entry, "invalid_arguments", entry.payload, body)
        else:
            await self._run(entry, task, body)

    async def _claim(self, entry: _Entry, body: dict[str, Any]) -> bool:
        """Whether the run of `body` is this entry's: the first entry to carry it claims it until
        its deadline, and only that entry (taken over by another worker, too) runs it."""
        ttl = max(1, math.ceil((body["until"] - time.time()) * 1000))
        key = f"{self._runs}{body['run']}"
        return bool(await self._eval("task_claim", CLAIM_SCRIPT, [key], entry.id, str(ttl)))

    async def _run(self, entry: _Entry, task: _Task, body: dict[str, Any]) -> None:
        assert self._resolved is not None  # noqa: S101  # resolved by start()
        args, kwargs = body["args"], body["kwargs"]
        tracer = self._resolved.tracer
        started = time.monotonic()
        with tracer.task(
            self.name, task.name, body["id"], body["attempt"], body.get("trace")
        ) as span:
            try:
                if _is_async(task.func):
                    await task.func(*args, **kwargs)
                else:
                    await asyncio.to_thread(task.func, *args, **kwargs)
            except asyncio.CancelledError:
                # stop() ran out of grace: back to the queue now, not after the visibility timeout.
                self._ran(task, "interrupted", started)
                await self._retry(entry, self._seal(self._dump({**body, **self._delivery(0)})), 0)
                raise
            except Exception as exc:
                record_failure(span, exc)
                retried = body["attempt"] < task.retries
                self._ran(task, "retried" if retried else "failed", started)
                await self._fail(entry, task, body, exc)
            else:
                self._ran(task, "succeeded", started)
                await self._eval("task_ack", ACK_SCRIPT, [self._stream], _GROUP, entry.id)

    def _ran(self, task: _Task, outcome: str, started: float) -> None:
        assert self._resolved is not None  # noqa: S101  # resolved by start()
        metrics, seconds = self._resolved.metrics, time.monotonic() - started
        metrics.count("tasks", queue=self.name, task=task.name, outcome=outcome)
        metrics.record("tasks.duration", seconds, queue=self.name, task=task.name, outcome=outcome)

    async def _fail(self, entry: _Entry, task: _Task, body: dict[str, Any], exc: Exception) -> None:
        failures = body["attempt"] + 1
        error = _error_type(exc)
        if failures > task.retries:
            logger.error(
                "Task %r (%s) failed with %s after %d attempts: sent to the dead letters",
                task.name,
                body["id"],
                error,
                failures,
                exc_info=exc,
            )
            await self._bury(entry, "failed", entry.payload, body, error=error, attempts=failures)
            return
        delay = task.delay(failures)
        logger.warning(
            "Task %r (%s) failed with %s, attempt %d of %d: retrying in %.1f s",
            task.name,
            body["id"],
            error,
            failures,
            task.retries + 1,
            delay,
            exc_info=exc,
        )
        retry = {**body, "attempt": failures, **self._delivery(delay)}
        await self._retry(entry, self._seal(self._dump(retry)), delay)

    async def _retry(self, entry: _Entry, payload: bytes, delay: float) -> None:
        keys = [self._stream, self._delayed]
        due = repr(time.time() + delay)
        await self._eval("task_retry", RETRY_SCRIPT, keys, _GROUP, entry.id, due, payload)

    async def _refuse(
        self,
        entry: _Entry,
        reason: str,
        payload: bytes,
        body: dict[str, Any] | None = None,
    ) -> None:
        # The name is %r: without a key, it's whatever someone wrote to the backend.
        logger.warning(
            "Refused a task (%r) of the queue %r: %s. Sent to the dead letters",
            body["task"] if body is not None else None,
            self.name,
            _REASONS[reason],
        )
        assert self._resolved is not None  # noqa: S101  # resolved by start()
        # The task's name only when it's registered: anyone could have written any other.
        known = {"task": body["task"]} if body is not None and body["task"] in self._tasks else {}
        self._resolved.metrics.count("tasks", queue=self.name, outcome=reason, **known)
        attempts = body["attempt"] if body is not None else 0
        await self._bury(entry, reason, payload, body, attempts=attempts)

    async def _bury(
        self,
        entry: _Entry,
        reason: str,
        payload: bytes,
        body: dict[str, Any] | None,
        *,
        error: str = "",
        attempts: int,
    ) -> None:
        await self._eval(
            "task_bury",
            BURY_SCRIPT,
            [self._stream, self._dead],
            _GROUP,
            entry.id,
            str(self.max_dead_letters),
            payload,
            reason,
            error,
            body["task"] if body is not None else "",
            body["id"] if body is not None else "",
            str(attempts),
            repr(time.time()),
        )

    # endregion

    # region Introspection

    async def pending(self) -> int:
        """How many tasks haven't finished: waiting for a worker, running, or waiting for their
        time (delayed ones and retries)."""
        self._ready()
        keys = [self._stream, self._delayed]
        return int(await self._eval("task_pending", PENDING_SCRIPT, keys))

    async def dead_letters(self, count: int = 100) -> list[DeadLetter]:
        """The latest `count` dead letters, newest first."""
        self._ready()
        entries = await self._call("task_dead_letters", "xrevrange", self._dead, "+", "-", count)
        return [_dead_letter(entry_id, fields) for entry_id, fields in entries]

    async def requeue_dead_letters(self, ids: Iterable[str] | None = None) -> int:
        """Put dead letters back in the queue (all of them by default) with their attempts
        reset, and return how many. Those whose task can't run here stay: tampered, malformed or
        too large, or named after no task of this process. Raises QueueFull when the queue fills
        up (those requeued until then stay requeued)."""
        self._ready()
        dead_ids = None if ids is None else _stream_ids(ids)
        requeued = 0
        async with contextlib.aclosing(self._dead_entries(dead_ids)) as entries:
            async for dead_id, fields in entries:
                body = self._open(fields.get(b"p", b""))
                if isinstance(body, str):
                    reason = body
                elif body["task"] not in self._tasks:
                    reason = "unknown_task"
                else:
                    requeued += await self._requeue(dead_id, body)
                    continue
                logger.warning(
                    "The dead letter %s of the queue %r stays: %s",
                    dead_id,
                    self.name,
                    _REASONS[reason],
                )
        if requeued:
            self._wakeup.set()
        return requeued

    async def discard_dead_letters(self, ids: Iterable[str]) -> int:
        """Delete these dead letters; returns how many there were."""
        self._ready()
        dead_ids = _stream_ids(ids)
        if not dead_ids:
            return 0
        return int(await self._call("task_discard", "xdel", self._dead, *dead_ids))

    async def _requeue(self, dead_id: str, body: dict[str, Any]) -> int:
        payload = self._seal(self._dump({**body, "attempt": 0, **self._delivery(0)}))
        keys = [self._dead, self._stream, self._delayed]
        result = int(
            await self._eval(
                "task_requeue", REQUEUE_SCRIPT, keys, dead_id, str(self.max_length), payload
            )
        )
        if result < 0:
            raise QueueFull(f"the task queue {self.name!r} already holds {self.max_length} tasks")
        return result

    async def _dead_entries(
        self,
        ids: list[str] | None,
    ) -> AsyncGenerator[tuple[str, dict[bytes, bytes]], None]:
        if ids is not None:
            for dead_id in ids:
                for entry_id, fields in await self._xrange(dead_id, dead_id, 1):
                    yield _text(entry_id), fields
            return
        start = "-"
        while True:
            page = await self._xrange(start, "+", _PAGE)
            for entry_id, fields in page:
                yield _text(entry_id), fields
            if len(page) < _PAGE:
                return
            start = "(" + _text(
                page[-1][0]
            )  # after the last one: those that stay aren't seen again

    async def _xrange(self, start: str, end: str, count: int) -> Any:
        return await self._call("task_dead_letters", "xrange", self._dead, start, end, count)

    # endregion
