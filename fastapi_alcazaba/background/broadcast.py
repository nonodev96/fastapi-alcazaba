"""Messages to every process: what one process must tell the WebSockets of the others (a chat, a
live board, notifications), or the processes themselves (a setting changed).

    broadcast = FastAPIBroadcast(redis_client, signing_key=settings.broadcast_key)

    await broadcast.publish("room:7", {"from": "ana", "text": "hi"})

    @app.websocket("/rooms/{room}")
    async def room(websocket: WebSocket, room: int) -> None:
        await websocket.accept()
        async with broadcast.subscribe(f"room:{room}") as messages:
            async for message in messages:
                if message.gap:
                    ...  # messages may have been missed: send the room's state again
                else:
                    await websocket.send_json(message.data)

Each process holds one subscription to Redis (or Valkey) for all its subscribers, and hands each
message to theirs. Without a client, messages stay in the process: for tests, and apps of one
process.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi_alcazaba.core.integrity import Secrets, secret_list

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: The longest message, as JSON: a broadcast isn't for files.
MAX_MESSAGE = 64 * 1024
_CHANNEL = re.compile(r"[A-Za-z0-9_.:\-]{1,200}")
_READ_TIMEOUT = 1.0  # seconds a read of the subscription waits, to notice a close
_PING_AFTER = 15.0  # seconds of silence before a ping, to tell a quiet channel from a dead one
_LOST_AFTER = 30.0


@dataclass(frozen=True, slots=True)
class Message:
    """A message of a channel, as subscribers get it.

    :param channel: the channel it was published to.
    :param data: what was published (JSON), None for a gap.
    :param gap: messages may have been missed before this one (the subscription to Redis
        dropped, or this subscriber fell behind): show the state again, from where it's kept.
    """

    channel: str
    data: Any = None
    gap: bool = False


class _Subscriber:
    def __init__(self, channel: str, size: int) -> None:
        self.channel = channel
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=size)

    def put(self, message: Message) -> None:
        """Queue `message`; a subscriber that fell behind loses its oldest, and is told."""
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.queue.put_nowait(Message(self.channel, gap=True))
            self.queue.put_nowait(message)


class FastAPIBroadcast:
    """Publishes messages to channels, which every process's subscribers of the channel get.

    - **At most once:** a message is for those subscribed when it's published; one that a
      subscriber's connection missed doesn't come back, it gets a `gap` instead. For work that
      must happen, a task queue or an outbox.
    - **Signed:** with `signing_key`, each message carries an HMAC of its channel and data, and
      those without it (planted by whoever can write to Redis) are dropped.
    - **Bounded:** a message has `MAX_MESSAGE` bytes of JSON at most, and each subscriber
      `queue_size` messages waiting: past it, it loses the oldest and gets a gap.
    - **Reconnects** by itself, with a backoff, and tells every subscriber of the gap.

    :param client: a redis-py or valkey-py asyncio client (the one of your RedisBackend); None
        to keep messages in this process.
    :param signing_key: a secret of 32 bytes or more, the same in every process (a list rotates
        it: the first signs, all of them verify).
    :param prefix: what the channels are under in Redis.
    :param queue_size: messages a subscriber may have waiting.
    """

    def __init__(
        self,
        client: Any = None,
        *,
        signing_key: Secrets | None = None,
        prefix: str = "fastapi_alcazaba:broadcast:",
        queue_size: int = 1000,
    ) -> None:
        if client is not None and not (hasattr(client, "pubsub") and hasattr(client, "publish")):
            raise TypeError("the client is a redis-py or valkey-py asyncio client, with pubsub()")
        if queue_size < 2:
            raise ValueError("queue_size is 2 or more")
        self.client = client
        self._keys = secret_list(signing_key) if signing_key is not None else None
        self.prefix = prefix
        self.queue_size = queue_size
        self._subscribers: dict[str, set[_Subscriber]] = {}
        self._pubsub: Any = None
        self._listener: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()  # the channels to subscribe to changed
        self._closed = False

    async def publish(self, channel: str, data: Any) -> None:
        """Send `data` (JSON) to the subscribers of `channel`, in every process. Raises when
        Redis fails: the message isn't sent."""
        _check(channel)
        body = json.dumps(data, separators=(",", ":"), allow_nan=False)
        if len(body) > MAX_MESSAGE:
            raise ValueError(f"a message is {MAX_MESSAGE} bytes of JSON at most")
        if self.client is None:
            self._deliver(Message(channel, json.loads(body)))
            return
        envelope = {"data": body}
        if self._keys is not None:
            envelope["mac"] = self._mac(self._keys[0], channel, body)
        await self.client.publish(self.prefix + channel, json.dumps(envelope))

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[AsyncIterator[Message]]:
        """The messages of `channel` from now on, for as long as the block lasts."""
        _check(channel)
        subscriber = _Subscriber(channel, self.queue_size)
        first = channel not in self._subscribers
        self._subscribers.setdefault(channel, set()).add(subscriber)
        if self.client is not None:
            self._start()
            if first:
                await self._follow()
        try:
            yield _messages(subscriber)
        finally:
            subscribers = self._subscribers.get(channel, set())
            subscribers.discard(subscriber)
            if not subscribers:
                self._subscribers.pop(channel, None)
                if self.client is not None:
                    await self._follow()

    async def close(self) -> None:
        """Stop listening (in the lifespan, at shutdown)."""
        self._closed = True
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await listener
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()

    # The subscription to Redis, one per process.

    def _start(self) -> None:
        self._closed = False
        if self._listener is None or self._listener.done():
            self._listener = asyncio.get_running_loop().create_task(self._listen())

    async def _follow(self) -> None:
        """Subscribe the shared connection to the channels with subscribers, and only those."""
        pubsub = self._pubsub
        if pubsub is None:
            return  # the listener subscribes to them when it connects
        wanted = {self.prefix + channel for channel in self._subscribers}
        current = {name.decode() if isinstance(name, bytes) else name for name in pubsub.channels}
        try:
            if wanted - current:
                await pubsub.subscribe(*(wanted - current))
            if current - wanted:
                await pubsub.unsubscribe(*(current - wanted))
        except Exception:  # the listener reconnects, and subscribes again
            logger.warning("Couldn't change the broadcast's subscriptions", exc_info=True)

    async def _listen(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = 0.5
        while not self._stopping():
            pubsub = self._pubsub = self.client.pubsub()
            try:
                await pubsub.subscribe(self.prefix + "__alcazaba__")  # keeps the connection open
                await self._follow()
                heard, pinged = loop.time(), False
                while not self._stopping():
                    message = await pubsub.get_message(timeout=_READ_TIMEOUT)
                    now = loop.time()
                    if message is None:
                        if now - heard >= _LOST_AFTER:
                            raise ConnectionError("the broadcast's subscription stopped answering")
                        if now - heard >= _PING_AFTER and not pinged:
                            await pubsub.ping()
                            pinged = True
                        continue
                    heard, pinged = now, False
                    if message.get("type") == "message":
                        self._received(message.get("channel"), message.get("data"))
                    backoff = 0.5
            except Exception:  # CancelledError, a BaseException, goes through: close() was called
                logger.warning("Lost the broadcast's subscription: reconnecting", exc_info=True)
            finally:
                self._pubsub = None
                with contextlib.suppress(Exception):
                    await pubsub.aclose()
            if self._stopping():
                return
            self._gap()  # what was published meanwhile is lost
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def _stopping(self) -> bool:
        """Whether `close` was called (a method: the flag changes while the listener waits)."""
        return self._closed

    def _received(self, name: Any, raw: Any) -> None:
        channel = name.decode() if isinstance(name, bytes) else str(name)
        if not channel.startswith(self.prefix):
            return
        channel = channel[len(self.prefix) :]
        try:
            envelope = json.loads(raw)
            body = envelope["data"]
            if not isinstance(body, str) or len(body) > MAX_MESSAGE:
                raise ValueError("not a message")
            if self._keys is not None and not any(
                hmac.compare_digest(str(envelope.get("mac", "")), self._mac(key, channel, body))
                for key in self._keys
            ):
                raise ValueError("not signed with the key")
            data = json.loads(body)
        except (TypeError, ValueError, KeyError):
            logger.warning("Dropped a broadcast message of %r that isn't ours", channel[:100])
            return
        self._deliver(Message(channel, data))

    def _deliver(self, message: Message) -> None:
        for subscriber in list(self._subscribers.get(message.channel, ())):
            subscriber.put(message)

    def _gap(self) -> None:
        for channel, subscribers in self._subscribers.items():
            for subscriber in list(subscribers):
                subscriber.put(Message(channel, gap=True))

    @staticmethod
    def _mac(key: bytes, channel: str, body: str) -> str:
        message = json.dumps([channel, body]).encode()
        return hmac.new(key, message, hashlib.sha256).hexdigest()


async def _messages(subscriber: _Subscriber) -> AsyncIterator[Message]:
    while True:
        yield await subscriber.queue.get()


def _check(channel: str) -> None:
    if not isinstance(channel, str) or not _CHANNEL.fullmatch(channel):
        raise ValueError("a channel is 1 to 200 letters, digits and '_.:-'")
