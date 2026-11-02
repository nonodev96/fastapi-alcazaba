# Passkeys

!!! warning "Experimental"
    New in 0.6: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

Logins without a password (Touch ID, Windows Hello, a phone, a security key), which a phishing
page can't relay, and a second factor that resists it: WebAuthn, by `FastAPIWebAuthn`, with the
`crypto` extra.

In an app: [the example](../examples/passkeys.md). Every class and function: [the API reference](../reference/auth.md#fastapi_alcazaba.auth.FastAPIWebAuthn).

```python
from fastapi_alcazaba import FastAPIWebAuthn

webauthn = FastAPIWebAuthn(rp_id="example.com", rp_name="Example", origins=["https://example.com"])
```

- **`rp_id`** is the site's domain (or one it's under: `example.com` for
  `https://login.example.com`); the passkeys only work there. **`origins`** are where the
  answers come from: `https://…`, or `http://localhost:8000` in development.
- **In the browser,** each ceremony is two requests: the options, which the page gives to
  `navigator.credentials.create()` (or `get()`) after
  `PublicKeyCredential.parseCreationOptionsFromJSON()` (or `parseRequestOptionsFromJSON()`), then
  `credential.toJSON()`, which the server verifies.

## Registering a passkey

```python
@app.post("/passkeys/options")
async def options(user: CurrentUser) -> dict[str, Any]:
    existing = [Passkey.from_dict(p) for p in user.passkeys]
    return await webauthn.registration_options(
        user.id, user.email, user_handle=user.handle, exclude=existing
    )


@app.post("/passkeys")
async def register(request: Request, credential: dict[str, Any], user: CurrentUser) -> None:
    try:
        passkey = await webauthn.verify_registration(user.id, credential, request=request)
    except WebAuthnRefused:
        raise HTTPException(400, "That passkey wasn't accepted") from None
    await db.passkeys.add(user.id, passkey.to_dict())  # its id must be no one else's
```

- **The user handle** is what the authenticator keeps for the user: random, not their email, and
  the same for all their passkeys. Without `user_handle`, the options have a new one
  (`options["user"]["id"]`); store it with the user. `exclude` keeps an authenticator from
  registering twice.
- **A passkey** (`residentKey: "required"`, the default) logs in without a username; the user is
  verified by the authenticator (`user_verification="required"`: a PIN, a fingerprint, the
  phone's lock), so a passkey is both factors.
- **What to store:** `passkey.to_dict()`, JSON: its id, its COSE public key, its algorithm
  (ES256, EdDSA or RS256), the user handle, its counter, and whether it's synced
  (`backup_eligible`, `backed_up`). Nothing in it opens a door: it's a public key.
- **Attestation** isn't checked: `attestation: "none"` is asked, so the app doesn't learn the
  authenticator's make. A login's security doesn't rest on it.

## Logging in

```python
@app.post("/login/options")
async def login_options() -> dict[str, Any]:
    return await webauthn.authentication_options()  # any passkey of this site


@app.post("/login")
async def login(request: Request, credential: dict[str, Any]) -> dict[str, str]:
    stored = await db.passkeys.get(credential_id(credential))  # and its user
    try:
        passkey = await webauthn.verify_authentication(
            credential, Passkey.from_dict(stored.passkey), user_id=stored.user_id, request=request
        )
    except WebAuthnRefused:
        raise HTTPException(401, "That passkey wasn't accepted") from None
    await db.passkeys.update(passkey.to_dict())  # its new counter
    sessions.login(request, stored.user_id)
    webauthn.mark(request, stored.user_id)  # a recent second factor: step-up
    ...
```

- **Without a username:** `authentication_options()` lets any passkey of the site answer, and the
  browser asks which (or offers them in the username field, with `autocomplete="webauthn"`). The
  answer's user handle must be the passkey's. With `user_id` and `allow=[...]`, only theirs: a
  second factor after a password.
- **What's checked** (WebAuthn Level 2, 7.1 and 7.2): the challenge (random, kept for `timeout`
  seconds, for this ceremony and this user, used once); the origin (one of `origins`, not in a
  cross-origin iframe); the RP ID's hash, which the authenticator signs, so a phishing site's
  answer is refused; the user present and verified; the signature, with the stored public key; and
  the counter.
- **A cloned authenticator:** a counter that goes back, or doesn't move for one that counts, is
  refused. Synced passkeys count 0 and aren't checked; their backup eligibility mustn't change.
- **Refused** answers raise `WebAuthnRefused`, whose `reason` (challenge, origin, rp_id,
  user_verification, signature, counter…) goes to the logs and the metrics, not to the user.
- **When the backend fails,** no answer is checked: the challenges can't be used once.
- **Step-up:** `webauthn.mark(request, user_id)` and `webauthn.require_recent(current_user_id,
  max_age=300)` are those of [the second factor](two-factor.md#step-up): a passkey or a TOTP code
  of the last minutes, in this session.

## Implemented here

The CBOR of attestation objects and COSE keys is read by the library itself, strictly: definite
lengths only, no tags nor floats, no duplicate keys, nothing after the item. The keys are checked
for their algorithm (P-256 for ES256, Ed25519 for EdDSA, RSA of 2048 bits or more for RS256) and
the signatures verified with cryptography. The tests run the answers of real browsers and
authenticators, besides a software authenticator that breaks each rule in turn.

With `audit=`, each passkey registered is an `auth.passkey.register` event and each login an
`auth.passkey` one, with the outcome; counted in `fastapi_alcazaba.webauthn`, by ceremony and
outcome.
