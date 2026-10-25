import math
import time
from typing import TYPE_CHECKING, Any

from aiobotocore.client import AioBaseClient
from aiobotocore.session import AioSession, get_session
from botocore.exceptions import ClientError

from fastapi_alcazaba.types import Backend

if TYPE_CHECKING:
    from types_aiobotocore_dynamodb import DynamoDBClient
else:
    DynamoDBClient = AioBaseClient


class DynamoBackend(Backend):
    """
    Amazon DynamoDB backend provider

    This backend requires an existing table within your AWS environment to be passed during
    backend init. If ttl is going to be used, this needs to be manually enabled on the table
    using the `ttl` key. Dynamo will take care of deleting outdated objects, but this is not
    instant so don't be alarmed when they linger around for a bit.

    As with all AWS clients, credentials will be taken from the environment. Check the AWS SDK
    for more information.

    Usage:
        >> dynamodb = DynamoBackend(table_name="your-cache", region="eu-west-1")
        >> await dynamodb.init()
        >> FastAPICache.init(dynamodb)
    """

    supports_locks = True
    supports_counters = True

    def __init__(
        self, table_name: str, region: str | None = None, *, endpoint_url: str | None = None
    ) -> None:
        """`endpoint_url` points to DynamoDB Local, LocalStack or a VPC endpoint."""
        self.session: AioSession = get_session()
        self.table_name = table_name
        self.region = region
        self.endpoint_url = endpoint_url
        self.client: DynamoDBClient | None = None

    async def init(self) -> None:
        self.client = await self.session.create_client(
            "dynamodb", region_name=self.region, endpoint_url=self.endpoint_url
        ).__aenter__()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.__aexit__(None, None, None)
            self.client = None

    def _get_client(self) -> DynamoDBClient:
        if self.client is None:
            raise RuntimeError("DynamoBackend.init() must be awaited first")
        return self.client

    async def get_with_ttl(self, key: str) -> tuple[int, bytes | None]:
        response = await self._get_client().get_item(
            TableName=self.table_name, Key={"key": {"S": key}}
        )
        item = response.get("Item")
        if item is None:
            return 0, None
        value = item.get("value", {}).get("B")
        ttl = item.get("ttl", {}).get("N")
        if value is None:
            return 0, None
        if not ttl:
            return -1, value
        # DynamoDB deletes expired items eventually, not right away: check it ourselves.
        remaining = int(ttl) - int(time.time())
        return (remaining, value) if remaining > 0 else (0, None)

    async def get(self, key: str) -> bytes | None:
        return (await self.get_with_ttl(key))[1]

    async def set(self, key: str, value: bytes, expire: int | None = None) -> None:
        item: dict[str, dict[str, str | bytes]] = {"key": {"S": key}, "value": {"B": value}}
        if expire:
            item["ttl"] = {"N": str(int(time.time()) + expire)}
        await self._get_client().put_item(TableName=self.table_name, Item=item)

    async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
        """Counters live in the `count` attribute. An expired counter that DynamoDB hasn't deleted
        yet goes on counting (the rate limiter never reuses one: its keys change every window)."""
        return await self._add(key, amount, expire)

    async def _add(
        self, key: str, amount: int, expire: int | None, *, at_most: int | None = None
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
        self, key: str, previous_key: str, *, amount: int, limit: int, weight: float, expire: int
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
        if namespace:
            raise NotImplementedError("DynamoDB can't delete a namespace without a table scan")
        if key:
            response = await self._get_client().delete_item(
                TableName=self.table_name, Key={"key": {"S": key}}, ReturnValues="ALL_OLD"
            )
            return 1 if response.get("Attributes") else 0
        return 0


def _condition_failed(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
