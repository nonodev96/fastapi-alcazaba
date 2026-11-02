# Data Model: refresh tokens

## Refresh token (held by the client)

```text
r1.<family>.<generation>.<mac>
```

| Part | Value |
| --- | --- |
| `r1` | format version |
| family | 22 URL-safe characters, random (`secrets.token_urlsafe(16)`); also the access tokens' `sid` |
| generation | decimal, 0 at login, +1 per rotation |
| mac | URL-safe base64 (no padding) of HMAC-SHA256(secret, JSON [family, subject, salt, generation]) |

At most 200 characters; anything else is `malformed`.

## Family record (in the backend)

Key: `{prefix}!refresh:family:<sha256 of the family id>`; JSON, sealed with the cache's key when it
has one.

| Field | Meaning |
| --- | --- |
| `sub` | the subject |
| `client` | the client it's bound to, or null |
| `salt` | 32 random bytes, base64 |
| `claims` | extra claims put in every access token of the family |
| `created` | Unix seconds of the login |
| `generation` | current generation |
| `rotated` | Unix seconds (float) of the last rotation |
| `used` | Unix seconds of the last exchange (idle lifetime) |

Expiry in the backend: `min(created + lifetime, used + idle)`.

## Subject cutoff

Key: `{prefix}!refresh:subject:<sha256 of the subject>`; the Unix second of `revoke_subject`;
families created before it are refused. Expires after `lifetime`.

## Blocklist session mark

Key: `{prefix}!revoked:session:<sha256 of the sid>` (TokenBlocklist); lasts the access tokens'
lifetime plus the blocklist's leeway.

## Exchange outcomes

| Presented token | Outcome |
| --- | --- |
| malformed | refused `malformed` |
| family unknown, expired or revoked | refused `unknown` |
| MAC doesn't match | refused `forged` (nothing revoked) |
| created before the subject's cutoff, or past `created + lifetime` | refused `revoked` / `expired` |
| other client | family revoked, refused `wrong_client` |
| current generation | rotated: new pair (`rotated`) |
| previous generation within `grace` | current refresh token again, new access token (`grace`) |
| any older generation, or previous after `grace` | family revoked, refused `reused` |
