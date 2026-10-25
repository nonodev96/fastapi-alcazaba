# API reference

::: fastapi_alcazaba.Cache

::: fastapi_alcazaba.get_cache

::: fastapi_alcazaba.FastAPICache

::: fastapi_alcazaba.CacheOptions

::: fastapi_alcazaba.decorator.cache

::: fastapi_alcazaba.key_builder.default_key_builder

## Rate limiting

::: fastapi_alcazaba.limiter.FastAPIRateLimiter

::: fastapi_alcazaba.limiter.client_ip

::: fastapi_alcazaba.limiter.client_network

::: fastapi_alcazaba.limiter.too_many_requests

## Locks

::: fastapi_alcazaba.locks.Lock

::: fastapi_alcazaba.locks.LockNotAcquired

## Idempotency keys

::: fastapi_alcazaba.idempotency.IdempotencyMiddleware

::: fastapi_alcazaba.idempotency.require_idempotency_key

::: fastapi_alcazaba.idempotency.authorization

## Revoked tokens and one-time codes

::: fastapi_alcazaba.tokens.TokenBlocklist

::: fastapi_alcazaba.tokens.OneTimeCodes

## Outbound calls

::: fastapi_alcazaba.outbound.ResilientTransport

::: fastapi_alcazaba.outbound.CircuitOpenError

::: fastapi_alcazaba.outbound.ResponseTooLargeError

## Middlewares

::: fastapi_alcazaba.middleware.SecurityHeadersMiddleware

::: fastapi_alcazaba.middleware.BodySizeLimitMiddleware

::: fastapi_alcazaba.middleware.DEFAULT_SECURITY_HEADERS

## Health checks

::: fastapi_alcazaba.health.health_router

::: fastapi_alcazaba.health.HealthCheck

## Scheduled jobs

::: fastapi_alcazaba.scheduler.FastAPIScheduler

## Circuit breakers

::: fastapi_alcazaba.breaker.FastAPIBreaker

::: fastapi_alcazaba.breaker.BreakerOpen

## Pagination

::: fastapi_alcazaba.pagination.FastAPIPagination

::: fastapi_alcazaba.pagination.FastAPIPaginationLimitOffset

::: fastapi_alcazaba.pagination.FastAPIPaginationCursor

::: fastapi_alcazaba.pagination.PageParams

::: fastapi_alcazaba.pagination.LimitOffsetParams

::: fastapi_alcazaba.pagination.CursorParams

::: fastapi_alcazaba.pagination.Page

::: fastapi_alcazaba.pagination.LimitOffsetPage

::: fastapi_alcazaba.pagination.CursorPage

## Coders

::: fastapi_alcazaba.coder.Coder

::: fastapi_alcazaba.coder.JsonCoder

::: fastapi_alcazaba.coder.PickleCoder

## Backends

::: fastapi_alcazaba.types.Backend

::: fastapi_alcazaba.backends.inmemory.InMemoryBackend

::: fastapi_alcazaba.backends.redis.RedisBackend

::: fastapi_alcazaba.backends.valkey.ValkeyBackend

::: fastapi_alcazaba.backends.glide.GlideBackend

::: fastapi_alcazaba.backends.memcached.MemcachedBackend

::: fastapi_alcazaba.backends.dynamodb.DynamoBackend

::: fastapi_alcazaba.backends.tiered.TieredBackend

## Resilience, integrity and errors

::: fastapi_alcazaba.resilience.CircuitBreaker

::: fastapi_alcazaba.integrity.Signer

::: fastapi_alcazaba.encryption.Encrypter

::: fastapi_alcazaba.errors
