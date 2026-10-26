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

## API keys

::: fastapi_alcazaba.apikeys.FastAPIAPIKeys

::: fastapi_alcazaba.apikeys.APIKeyRecord

## Signed webhooks

::: fastapi_alcazaba.webhooks.FastAPIWebhooks

::: fastapi_alcazaba.webhooks.WebhookSigner

::: fastapi_alcazaba.webhooks.WebhookMessage

## Outbound calls

::: fastapi_alcazaba.outbound.ResilientTransport

::: fastapi_alcazaba.outbound.CircuitOpenError

::: fastapi_alcazaba.outbound.ResponseTooLargeError

::: fastapi_alcazaba.outbound.SSRFGuardTransport

::: fastapi_alcazaba.outbound.BlockedDestinationError

## Middlewares

::: fastapi_alcazaba.middleware.SecurityHeadersMiddleware

::: fastapi_alcazaba.middleware.BodySizeLimitMiddleware

::: fastapi_alcazaba.middleware.DEFAULT_SECURITY_HEADERS

## CSRF protection

::: fastapi_alcazaba.csrf.CSRFMiddleware

## Server-side sessions

::: fastapi_alcazaba.sessions.ServerSessionMiddleware

::: fastapi_alcazaba.sessions.rotate_session

::: fastapi_alcazaba.sessions.clear_session

::: fastapi_alcazaba.sessions.ServerSession

## Checking uploads

::: fastapi_alcazaba.uploads.FastAPIUploads

::: fastapi_alcazaba.uploads.CheckedFile

::: fastapi_alcazaba.uploads.sniff

::: fastapi_alcazaba.uploads.image_size

::: fastapi_alcazaba.uploads.safe_filename

## Problem details

::: fastapi_alcazaba.problems.FastAPIProblemDetails

::: fastapi_alcazaba.problems.Problem

## Request IDs

::: fastapi_alcazaba.request_id.RequestIDMiddleware

::: fastapi_alcazaba.request_id.request_id

::: fastapi_alcazaba.request_id.RequestIDLogFilter

## Audit trail

::: fastapi_alcazaba.audit.FastAPIAudit

::: fastapi_alcazaba.audit.AuditEntry

::: fastapi_alcazaba.audit.AuditEvent

::: fastapi_alcazaba.audit.AuditSink

::: fastapi_alcazaba.audit.LoggingAuditSink

::: fastapi_alcazaba.audit.MemoryAuditSink

::: fastapi_alcazaba.audit.verify_audit_trail

::: fastapi_alcazaba.audit.SENSITIVE_WORDS

## Request timeouts

::: fastapi_alcazaba.timeouts.RequestTimeoutMiddleware

## Health checks

::: fastapi_alcazaba.health.health_router

::: fastapi_alcazaba.health.HealthCheck

## Scheduled jobs

::: fastapi_alcazaba.scheduler.FastAPIScheduler

## Task queue

::: fastapi_alcazaba.tasks.FastAPITaskQueue

::: fastapi_alcazaba.tasks.DeadLetter

::: fastapi_alcazaba.tasks.QueueFull

## Circuit breakers

::: fastapi_alcazaba.breaker.FastAPIBreaker

::: fastapi_alcazaba.breaker.BreakerOpen

## Concurrency limits

::: fastapi_alcazaba.concurrency.FastAPIConcurrencyLimiter

::: fastapi_alcazaba.concurrency.ConcurrencyLimitExceeded

## Leader election

::: fastapi_alcazaba.leader.FastAPILeader

## Maintenance mode

::: fastapi_alcazaba.maintenance.FastAPIMaintenance

::: fastapi_alcazaba.maintenance.MaintenanceMiddleware

## Feature flags

::: fastapi_alcazaba.flags.FastAPIFeatureFlags

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
