from valkey.asyncio.client import Valkey
from valkey.asyncio.cluster import ValkeyCluster

from fastapi_alcazaba.backends._redis_common import RedisLikeBackend


class ValkeyBackend(RedisLikeBackend):
    """Valkey through its own client, valkey-py. A redis-py client pointed at
    Valkey works too, with RedisBackend.

    Create the client without `decode_responses=True`: cached values are bytes.
    """

    def __init__(self, valkey: Valkey | ValkeyCluster, *, scan_count: int = 500) -> None:
        is_cluster = isinstance(valkey, ValkeyCluster)
        super().__init__(valkey, is_cluster=is_cluster, scan_count=scan_count)
        self.valkey = valkey
