import asyncio
import contextlib
import math
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from aiobotocore.client import AioBaseClient
from aiobotocore.session import AioSession, get_session
from botocore.exceptions import ClientError

from fastapi_alcazaba.backends._generations import (
    Stamp,
    alive,
    check_depth,
    namespace_generation,
    namespaces,
    new_token,
    tag_generation,
)
from fastapi_alcazaba.backends.base import Backend, is_state_key
from fastapi_alcazaba.core.errors import BackendUnavailable

if TYPE_CHECKING:
    from types_aiobotocore_dynamodb import DynamoDBClient
else:
    DynamoDBClient = AioBaseClient

Item = dict[str, Any]
# BatchGetItem may leave keys unprocessed when the table is throttled: retried this many times.
_BATCH_ATTEMPTS = 5


class DynamoBackend(Backend):
    """
    Amazon DynamoDB backend provider

    This backend requires an existing table within your AWS environment to be passed during
    backend init. If ttl is going to be used, this needs to be manually enabled on the table
    using the `ttl` key. Dynamo will take care of deleting outdated objects, but this is not
    instant so don't be alarmed when they linger around for a bit.

    As with all AWS clients, credentials will be taken from the environment. Check the AWS SDK
    for more information.

    `clear(namespace=...)` and `invalidate_tags` work without a scan: each namespace or tag that
    was cleared has a generation, an item of its own, and cached responses record the generations
    they were stored under (see `_generations`); a clear writes a new one, so the entries stored
    before it are no longer served. They can't be counted: both return 0. Reads are eventually
    consistent, so a clear may take a moment to be seen.

    Usage:
        >> dynamodb = DynamoBackend(table_name="your-cache", region="eu-west-1")
        >> await dynamodb.init()
        >> FastAPICache.init(dynamodb)
    """

    supports_locks = True
    supports_tags = True
    supports_counters = True

    def __init__(
        self,
        table_name: str,
        region: str | None = None,
        *,
        endpoint_url: str | None = None,
    ) -> None:
        """`endpoint_url` points to DynamoDB Local, LocalStack or a VPC endpoint."""
        self.session: AioSession = get_session()
        self.table_name = table_name
        self.region = region
        self.endpoint_url = endpoint_url
        self.client: DynamoDBClient | None = None
        self._exit_stack = contextlib.AsyncExitStack()  # closes the client

    async def init(self) -> None:
        self.client = await self._exit_stack.enter_async_context(
            self.session.create_client(
                "dynamodb", region_name=self.region, endpoint_url=self.endpoint_url
            )
        )

    async def close(self) -> None:
        self.client = None
        await self._exit_stack.aclose()

    def _get_client(self) -> DynamoDBClient:
        if self.client is None:
            raise RuntimeError("DynamoBackend.init() must be awaited first")
        return self.client

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        if is_state_key(key):
            return _value(await self._get_item(key))
        required = [namespace_generation(namespace) for namespace in namespaces(key)]
        found = await self._get_items([key, *required])
        item = found.pop(key)
        if item is None:
            return 0, None
        recorded = {**_tokens(item, "generations"), **_tokens(item, "tag_generations")}
        if others := [name for name in recorded if name not in found]:
            found.update(await self._get_items(others))
        current = {name: _generation(found_item) for name, found_item in found.items()}
        if not alive(Stamp(0, recorded), current, required):
            return 0, None
        return _value(item)

    async def get(self, key: str) -> bytes | None:
        return (await self.get_with_ttl(key))[1]

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        item: Item = {"key": {"S": key}, "value": {"B": value}}
        if expire:
            item["ttl"] = {"N": str(int(time.time()) + expire)}
        if not is_state_key(key):
            names = [namespace_generation(namespace) for namespace in namespaces(key)]
            if generations := await self._generations(names):
                item["generations"] = _token_map(generations)
        await self._get_client().put_item(TableName=self.table_name, Item=item)

    async def _get_item(self, key: str) -> Item | None:
        response = await self._get_client().get_item(
            TableName=self.table_name, Key={"key": {"S": key}}
        )
        return response.get("Item")

    async def _get_items(self, keys: Sequence[str]) -> dict[str, Item | None]:
        """The items of `keys` (None for those missing) in one request."""
        if len(keys) == 1:
            return {keys[0]: await self._get_item(keys[0])}
        found: dict[str, Item | None] = dict.fromkeys(keys)
        request: Any = {self.table_name: {"Keys": [{"key": {"S": key}} for key in keys]}}
        for attempt in range(_BATCH_ATTEMPTS):
            response = await self._get_client().batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(self.table_name, []):
                found[item["key"]["S"]] = item
            request = response.get("UnprocessedKeys")
            if not request:
                return found
            await asyncio.sleep(0.05 * 2**attempt)
        raise BackendUnavailable("DynamoDB kept leaving keys of a BatchGetItem unprocessed")

    async def _generations(self, names: Sequence[str]) -> dict[str, str]:
        """The current token of each of the generations `names` that exists."""
        if not names:
            return {}
        found = await self._get_items(names)
        return {name: token for name, item in found.items() if (token := _generation(item))}

    async def _renew(self, name: str) -> None:
        await self._get_client().put_item(
            TableName=self.table_name,
            Item={"key": {"S": name}, "generation": {"S": new_token()}},
        )

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        """Counters live in the `count` attribute. An expired counter that DynamoDB hasn't deleted
        yet goes on counting (the rate limiter never reuses one: its keys change every window)."""
        return await self._add(key, amount, expire)

    async def _add(
        self,
        key: str,
        amount: int,
        expire: int | None,
        *,
        at_most: int | None = None,
    ) -> int:
        """Add `amount` to the counter `key`; with `at_most`, only if it holds no more than that
        (ConditionalCheckFailedException otherwise)."""
        values: dict[str, dict[str, str]] = {":amount": {"N": str(amount)}}
        update = "ADD #count :amount"
        if expire:
            update += " SET #ttl = if_not_exists(#ttl, :ttl)"
            values[":ttl"] = {"N": str(int(time.time()) + expire)}
        condition: dict[str, Any] = {}
        if at_most is not None:
            condition["ConditionExpression"] = "attribute_not_exists(#count) OR #count <= :at_most"
            values[":at_most"] = {"N": str(at_most)}
        response = await self._get_client().update_item(
            TableName=self.table_name,
            Key={"key": {"S": key}},
            UpdateExpression=update,
            # COUNT and TTL are reserved words.
            ExpressionAttributeNames={"#count": "count", **({"#ttl": "ttl"} if expire else {})},
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
            **condition,
        )
        return int(response["Attributes"]["count"]["N"])

    async def get_counter(self, key: str) -> int:
        # A strongly consistent read: the limiter must see the latest requests.
        response = await self._get_client().get_item(
            TableName=self.table_name, Key={"key": {"S": key}}, ConsistentRead=True
        )
        item = response.get("Item") or {}
        count = item.get("count", {}).get("N")
        ttl = item.get("ttl", {}).get("N")
        if count is None or (ttl and int(ttl) <= int(time.time())):
            return 0
        return int(count)

    async def incr_window(
        self,
        key: str,
        previous_key: str,
        *,
        amount: int,
        limit: int,
        weight: float,
        expire: int,
    ) -> tuple[bool, int, int]:
        """Atomic for the current window: a conditional update. The previous window is read first;
        it no longer changes (but for clients with a clock behind)."""
        previous = await self.get_counter(previous_key)
        room = limit - previous * weight - amount
        if room >= 0:
            try:
                current = await self._add(key, amount, expire, at_most=math.floor(room))
            except ClientError as exc:
                if not _condition_failed(exc):
                    raise
            else:
                return True, current, previous
        return False, await self.get_counter(key), previous

    # Locks are items with the holder's token and their expiry (`lock_expires`, Unix time in
    # milliseconds), taken and released with conditional writes.

    async def acquire_lock(self, key: str, token: str, ttl: float) -> bool:
        now = time.time()
        expires = now + ttl
        try:
            await self._get_client().put_item(
                TableName=self.table_name,
                Item={
                    "key": {"S": key},
                    "token": {"S": token},
                    "lock_expires": {"N": str(int(expires * 1000))},
                    "ttl": {"N": str(math.ceil(expires))},
                },
                ConditionExpression="attribute_not_exists(#key) OR #expires < :now",
                ExpressionAttributeNames={"#key": "key", "#expires": "lock_expires"},
                ExpressionAttributeValues={":now": {"N": str(int(now * 1000))}},
            )
        except ClientError as exc:
            if not _condition_failed(exc):
                raise
            return False
        return True

    async def release_lock(self, key: str, token: str) -> None:
        try:
            await self._get_client().delete_item(
                TableName=self.table_name,
                Key={"key": {"S": key}},
                ConditionExpression="#token = :token",
                ExpressionAttributeNames={"#token": "token"},
                ExpressionAttributeValues={":token": {"S": token}},
            )
        except ClientError as exc:
            if not _condition_failed(exc):
                raise

    async def extend_lock(self, key: str, token: str, ttl: float) -> bool:
        now = time.time()
        expires = now + ttl
        try:
            await self._get_client().update_item(
                TableName=self.table_name,
                Key={"key": {"S": key}},
                UpdateExpression="SET #expires = :expires, #ttl = :ttl",
                ConditionExpression="#token = :token AND #expires >= :now",
                ExpressionAttributeNames={
                    "#token": "token",
                    "#expires": "lock_expires",
                    "#ttl": "ttl",
                },
                ExpressionAttributeValues={
                    ":token": {"S": token},
                    ":now": {"N": str(int(now * 1000))},
                    ":expires": {"N": str(int(expires * 1000))},
                    ":ttl": {"N": str(math.ceil(expires))},
                },
            )
        except ClientError as exc:
            if not _condition_failed(exc):
                raise
            return False
        return True

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        """Hide every entry of `namespace` and return 0 (they can't be counted without a scan), or
        delete the entry `key`."""
        if namespace is not None:
            check_depth(namespace)
            await self._renew(namespace_generation(namespace))
            return 0
        if key:
            # Conditional, so that of several deleting the item at once only one is told it was
            # there: unconditional deletes can each get it back (one-time codes rely on this).
            try:
                await self._get_client().delete_item(
                    TableName=self.table_name,
                    Key={"key": {"S": key}},
                    ConditionExpression="attribute_exists(#key)",
                    ExpressionAttributeNames={"#key": "key"},
                )
            except ClientError as exc:
                if not _condition_failed(exc):
                    raise
                return 0
            return 1
        return 0

    async def add_tags(self, key: str, tags: Sequence[str], expire: int | None) -> None:
        names = [tag_generation(tag) for tag in tags]
        current = await self._generations(names)
        # "" for a tag never invalidated: recorded all the same, so its first invalidation counts.
        tokens = {name: current.get(name, "") for name in names}
        try:
            await self._get_client().update_item(
                TableName=self.table_name,
                Key={"key": {"S": key}},
                UpdateExpression="SET #tags = :tags",
                ConditionExpression="attribute_exists(#key)",  # not if it's gone already
                ExpressionAttributeNames={"#tags": "tag_generations", "#key": "key"},
                ExpressionAttributeValues={":tags": _token_map(tokens)},
            )
        except ClientError as exc:
            if not _condition_failed(exc):
                raise

    async def invalidate_tags(self, tags: Sequence[str]) -> int:
        """Hide every entry of `tags` and return 0 (they can't be counted without a scan)."""
        for tag in tags:
            await self._renew(tag_generation(tag))
        return 0


def _value(item: Item | None) -> tuple[int, bytes | None]:
    """The value of an entry's item and its remaining lifetime."""
    if item is None:
        return 0, None
    value: bytes | None = item.get("value", {}).get("B")
    ttl = item.get("ttl", {}).get("N")
    if value is None:
        return 0, None
    if not ttl:
        return -1, value
    # DynamoDB deletes expired items eventually, not right away: check it ourselves.
    remaining = int(ttl) - int(time.time())
    return (remaining, value) if remaining > 0 else (0, None)


def _generation(item: Item | None) -> str | None:
    token: str | None = (item or {}).get("generation", {}).get("S")
    return token


def _tokens(item: Item, attribute: str) -> dict[str, str]:
    """A map of generation tokens the entry recorded."""
    recorded = item.get(attribute, {}).get("M", {})
    return {name: value.get("S", "") for name, value in recorded.items()}


def _token_map(tokens: dict[str, str]) -> dict[str, Any]:
    return {"M": {name: {"S": token} for name, token in tokens.items()}}


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
