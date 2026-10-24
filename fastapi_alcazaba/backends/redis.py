from redis.asyncio.client import Redis
from redis.asyncio.cluster import RedisCluster

from fastapi_alcazaba.backends._redis_common import RedisLikeBackend


class RedisBackend(RedisLikeBackend):
    """Redis, or any server speaking its protocol (Valkey, KeyDB, Dragonfly...), through redis-py.

    Create the client without `decode_responses=True`: cached values are bytes.
    """

    def __init__(self, redis: Redis | RedisCluster, *, scan_count: int = 500) -> None:
        super().__init__(redis, is_cluster=isinstance(redis, RedisCluster), scan_count=scan_count)
        self.redis = redis
