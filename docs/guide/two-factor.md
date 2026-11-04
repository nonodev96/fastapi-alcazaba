# Second factor

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

The codes of an authenticator app (Google Authenticator, 1Password, Authy, Bitwarden…) after the
password, recovery codes for a lost phone, and a recent code for sensitive actions: `FastAPITOTP`,
with the `crypto` extra, which seals the secrets.

In an app: [the example](../examples/two_factor.md). Every class and function: [the API reference](../reference/auth.md#fastapi_alcazaba.auth.FastAPITOTP).

```python
from fastapi_alcazaba import FastAPITOTP

totp = FastAPITOTP(issuer="Example", secret=settings.totp_secret, audit=audit)
```

## Adding the app

```python
@app.post("/totp/enroll")
async def enroll(request: Request, user: CurrentUser) -> dict[str, str]:
    enrollment = totp.enroll(user.id, label=user.email)
    request.session["enrolling"] = enrollment.sealed  # until a code of it is confirmed
    return {"uri": enrollment.uri, "secret": enrollment.secret.get_secret_value()}


@app.post("/totp/confirm")
async def confirm(request: Request, form: Code, user: CurrentUser) -> dict[str, list[str]]:
    sealed = request.session["enrolling"]
    if not await totp.verify(user.id, sealed, form.code, request=request):
        raise HTTPException(400, "Wrong code: try the next one")
    recovery = totp.recovery_codes(user.id)
    await db.users.update(user.id, totp=sealed, recovery=recovery.digests)
    return {"recovery_codes": recovery.codes}  # shown once
```

- **The secret:** 160 random bits (RFC 4226). `uri` is the `otpauth://` URI to show as a QR
  code (with a library such as `segno` or `qrcode`, in your page); `secret`, the same in base32,
  for those who type it.
- **Sealed, never in clear:** `sealed` is the secret encrypted with AES-256-GCM, bound to its
  user: store it, not the secret. It doesn't open for another user, nor without `secret`, so a
  copy of the database, or a sealed secret moved to another account, gives nobody codes. A list
  rotates `secret`: the first one seals, all of them open.
- **Confirmed first:** turn the second factor on once the user has typed a code of the new app,
  so that a QR code scanned wrong doesn't lock them out.

## Logging in

```python
@app.post("/login/totp")
async def login_totp(request: Request, form: Code) -> dict[str, str]:
    user = await db.users.get(request.session["pending"])  # set by the password step
    if not await totp.verify(user.id, user.totp, form.code, request=request):
        raise HTTPException(401, "Wrong code")
    log_in(request, user)
    totp.mark(request, user.id)  # the second factor is recent: step-up
    ...
```

- **The codes:** RFC 6238's, SHA1, 6 digits every 30 seconds, as every app does (`digits=8`,
  `algorithm="SHA256"`, `period=60` for those that support it). The current one is accepted, and
  `window=1` step before and after, for clocks out of step.
- **Each code once:** a code accepted is refused afterwards (RFC 6238, 5.2), in every process:
  someone who saw it over the user's shoulder, or a phishing page that relays it, gets nothing.
  Of two requests with the same code, one wins.
- **Guesses:** after `max_attempts` (5) wrong codes, recovery codes included, the user's codes
  are refused for `lockout` (15 minutes) from the first of them, the right one too. A right code
  forgives the wrong ones before it. The other users aren't affected.
- **Halfway in:** between the password and the code, keep in the session who's logging in, not
  that they're logged in: the routes for logged-in users don't take them yet.
- **When the backend fails,** nothing is accepted: no code is checked for replay, nor guesses
  counted. The backend must hold locks, which mark each code used: every backend shipped with
  fastapi-alcazaba does, and one of your own without them is refused.

## Recovery codes

```python
used = await totp.use_recovery_code(user.id, form.code, user.recovery, request=request)
if used is None:
    raise HTTPException(401, "Wrong code")
await db.users.update(user.id, recovery=[d for d in user.recovery if d != used])
```

- **Ten codes**, `XXXXX-XXXXX` of letters and digits that can't be mistaken for each other
  (about 50 bits each), typed in any case, with or without the dash. Shown once; the database
  keeps their digests (HMAC-SHA256 with `secret`, bound to the user).
- **Each once:** `use_recovery_code` returns the digest used, to delete; until it's deleted,
  another request with the same code is refused. Wrong ones count as guesses.
- **New ones** replace the old ones: `recovery_codes(user.id)` again, and store its digests
  instead.

## Step-up

```python
@app.delete(
    "/account",
    dependencies=[Depends(totp.require_recent(current_user_id, max_age=300))],
)
async def delete_account(...) -> None: ...
```

- **A recent code:** `require_recent` lets through the users who gave their second factor in
  this session in the last `max_age` seconds; the others get a 403 (`Second factor required`), to
  ask for a code, and requests without a user a 401. `totp.mark(request, user.id)` notes it,
  after `verify` or `use_recovery_code`, and gives the session a new id: it holds more
  privileges now.
- **Bound to the user:** the mark says whose it is; another user who logs in in the same browser
  session doesn't inherit it.
- **Sessions:** with `ServerSessionMiddleware` (or Starlette's `SessionMiddleware`).
- **Shared with passkeys:** a login with a verified [passkey](passkeys.md) marks the session the
  same way (`webauthn.mark`), and either `require_recent` takes a code or a passkey; both are
  `fastapi_alcazaba.auth.stepup`'s.

### By factor, and for tokens

```python
from fastapi_alcazaba.auth.stepup import require_recent

passkey_only = require_recent(current_user_id, max_age=300, methods={"passkey"}, audit=audit)

# API clients with the provider's tokens: its auth_time, amr and acr decide.
claims = auth.authenticate("oidc_tokens")  # returns the token's claims
recent_mfa = require_recent(
    current_user_id, max_age=600, claims=claims, methods={"hwk", "otp"}, acr_values={"urn:mfa"}
)
```

- **Which factor:** `methods` lists those that count: the method the session was marked with
  (`"passkey"`, `"totp"`, or yours: `stepup.mark(request, user_id, "password")`), or for tokens
  their `amr` values (RFC 8176: `"hwk"` a hardware key, `"otp"`, `"mfa"`…).
  `stepup.verified_with(request, user_id)` says which one the session holds.
- **The front end knows what to do:** a session that falls short gets a 403 problem
  (`type: "urn:fastapi-alcazaba:problem:step-up"`, with `max_age` and `methods`; install
  [`FastAPIProblemDetails`](observability.md#problem-details) to get it as
  `application/problem+json`).
- **Tokens:** with `claims=` (a dependency that returns the token's claims when there's one), the
  token must be the user's: its `sub` is what `current_user_id` returns (`subject=` reads another
  claim), so a fresh token of an attacker's own, sent with a stolen session, steps nobody up. Its
  `auth_time` must be within `max_age`, an `amr` among `methods`, and its `acr` among
  `acr_values`. Otherwise a 401 with RFC 9470's challenge:
  `WWW-Authenticate: Bearer error="insufficient_user_authentication", ..., max_age="600",
  acr_values="urn:mfa"`, for the client to send the user back to the provider with those.
- **Audited:** with `audit=`, each check is an `auth.stepup` event: the user, the method, how old,
  and the reason when it's refused (`none`, `too_old`, `wrong_method`, `no_auth_time`,
  `wrong_acr`, `other_subject`).

## In the audit trail

With `audit=`, each code checked is an `auth.mfa` event and each recovery code tried an
`auth.recovery_code` one, with the user, the outcome (the reason when it failed: wrong,
replayed, malformed, locked, unchecked), and the request's route and client. Never the code.
Counted in `fastapi_alcazaba.totp.verifications` and `fastapi_alcazaba.totp.recovery`, by
outcome: a burst of wrong or locked is someone guessing.
