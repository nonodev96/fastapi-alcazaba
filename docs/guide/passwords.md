# Passwords

!!! warning "Experimental"
    New in 0.7: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

Passwords from sign-up to reset: kept as slow hashes, checked against those known to be leaked,
guessed at most a few times per account, and reset by a link that works once. `FastAPIPasswords`,
with nothing to install: the hashes are scrypt's, from the standard library. The leak check needs
the `passwords` extra.

Every class and function: [the API reference](../reference/auth.md#fastapi_alcazaba.auth.FastAPIPasswords).

```python
from fastapi_alcazaba import FastAPIPasswords
from fastapi_alcazaba.auth import PwnedPasswords

passwords = FastAPIPasswords(
    pepper=settings.password_pepper,  # a SecretStr of 32 bytes or more
    pwned=PwnedPasswords(),
    audit=audit,
)
```

## Signing up

```python
@app.post("/signup")
async def signup(form: SignUp) -> dict[str, str]:
    problems = await passwords.check(form.password, user_inputs=[form.email, form.name])
    if problems:
        raise HTTPException(422, {"password": problems})
    user = await db.users.create(email=form.email, password=await passwords.hash(form.password))
    ...
```

- **What `check` refuses,** as NIST SP 800-63B says: fewer than `min_length` characters (15 by
  default, the length for a password that is the only factor; 8 with a second factor), more than
  1024, the e-mail address or the name in it, and a password among those leaked. No rules on its
  characters (a digit, a capital, a symbol): length is what makes a password strong.
- **Leaked passwords** are the first that attackers try. `PwnedPasswords` asks Have I Been Pwned
  without sending the password: only the first five characters of its SHA-1 go out, and the
  answer is padded, so that its size doesn't tell them either. When the API doesn't answer, the
  password is taken as not leaked and a warning logged (`fail_open=False` raises instead).
- **The hash:** scrypt (RFC 7914) with OWASP's parameters (128 MiB, a fifth of a second), a
  random salt each, in a thread, in PHC's format: `$scrypt$ln=17,r=8,p=1$<salt>$<hash>`.
- **The pepper:** the password is first an HMAC with it, so that a stolen database without the
  pepper gives nothing to guess against. Keep it out of the database, in your settings: it
  can't be rotated without the passwords.

## Logging in

```python
@app.post("/login")
async def login(request: Request, form: Login) -> dict[str, str]:
    user = await db.users.by_email(form.email)
    user_id, stored = (user.id, user.password) if user else (None, None)
    if not await passwords.verify(user_id, form.password, stored, request=request):
        raise HTTPException(401, "Wrong e-mail or password")
    if passwords.needs_rehash(user.password):
        await db.users.update(user.id, password=await passwords.hash(form.password))
    log_in(request, user)
    ...
```

- **Guesses per account:** after `max_attempts` wrong passwords (10), the account's logins are
  refused for `lockout` seconds (900), the right password too, in every process. Other accounts
  aren't affected. One client trying many accounts is a rate limit's:
  [one per IP on the login route](limits.md).
- **Unknown accounts take as long:** with `stored=None`, `verify` hashes against a made-up hash,
  so the time of an answer doesn't tell which e-mail addresses have an account. Answer both
  cases with the same message.
- **Stronger parameters later:** `needs_rehash` is true for a hash made with others than
  `parameters`; hash the password again while you have it.
- **When the backend fails,** the attempts can't be counted, and the password is refused.

## Resetting

```python
@app.post("/password/forgot")
async def forgot(form: Forgot) -> None:
    user = await db.users.by_email(form.email)
    if user is not None:
        token = await passwords.reset_token(user.id)
        await mail.send(user.email, "Reset your password", f"{settings.url}/reset?token={token}")
    # the same answer either way


@app.post("/password/reset")
async def reset(request: Request, form: Reset) -> None:
    if problems := await passwords.check(form.password):
        raise HTTPException(422, {"password": problems})
    user_id = await passwords.use_reset_token(form.token, request=request)
    if user_id is None:
        raise HTTPException(400, "The link expired or was used: ask for another")
    await db.users.update(user_id, password=await passwords.hash(form.password))
    await user_sessions.end_all(user_id)  # whoever knew the old password is logged out
```

- **A token, once:** 256 random bits, kept as a digest for `reset_ttl` seconds (an hour). Of two
  requests with it, one gets the user; a newer token replaces the user's last one.
- **The user ID comes back as a string,** as the backend keeps it.
- **Send it by e-mail** ([FastAPIMail](mail.md)), in the path or the body of the reset page's
  form, never in a page's links to other sites: the `Referer` would carry it.

## What's recorded

With `audit`, each password checked is an `auth.password` event, and each reset an
`auth.password_reset`, with the outcome as `reason` when it failed (wrong, locked, unknown,
replaced...). The metric `fastapi_alcazaba.passwords` counts the same by outcome: a burst of
wrong or locked is someone guessing. Neither ever has the password, nor the token.
