import time
from typing import TYPE_CHECKING

from aiobotocore.client import AioBaseClient
from aiobotocore.session import AioSession, get_session

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

    async def clear(self, namespace: str | None = None, key: str | None = None) -> int:
        if namespace:
            raise NotImplementedError("DynamoDB can't delete a namespace without a table scan")
        if key:
            response = await self._get_client().delete_item(
                TableName=self.table_name, Key={"key": {"S": key}}, ReturnValues="ALL_OLD"
            )
            return 1 if response.get("Attributes") else 0
        return 0
