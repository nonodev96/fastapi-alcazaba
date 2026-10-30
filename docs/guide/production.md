# Production setup

The [quick start](index.md) keeps everything in the process: each process has its own cache,
counters and revoked tokens. In production, several processes or containers serve the app, and
they share that state through a backend: Redis, Valkey, memcached or DynamoDB.

## Install the extras

Each backend and integration needs a library of its own, which an extra installs:

--8<-- "README.md:install"

## The app

An app as it runs in production: its configuration from the environment, the cache in Redis,
shared by every process, and its entries signed.

--8<-- "README.md:quick-start"

## Next steps

- [Backends](backends.md): Redis, Valkey, GLIDE, memcached, DynamoDB, and a copy in each process
  in front of them.
- [Security model and resilience](security-model.md): what gets cached and for whom, what the
  backend is trusted with, and what happens when it fails.
- [Metrics and traces](observability.md#metrics-and-traces): what Alcazaba records with
  OpenTelemetry, and the logs to configure.
