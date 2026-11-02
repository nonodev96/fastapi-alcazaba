# Quickstart: checking it end to end

```bash
.venv/bin/pytest tests/unit/test_refresh_tokens.py tests/unit/test_example_auth_jwt.py
REDIS_URL=redis://127.0.0.1:16379/15 MEMCACHED_URL=127.0.0.1:21211 \
  DYNAMODB_URL=http://127.0.0.1:18000 \
  .venv/bin/pytest -m integration tests/integration/test_backends_live.py -k refresh
```

By hand, with the JWT example (`uvicorn examples.auth_strategies.jwt_tokens:app`):

1. `POST /token` with octocat's password: an access token and a refresh token.
2. `POST /token/refresh` with the refresh token: a new pair.
3. The same refresh token again, after 10 seconds: 401, and the access token of step 2 gets 401
   on `GET /me`.
