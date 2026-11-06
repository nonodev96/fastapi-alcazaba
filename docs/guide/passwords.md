# Passwords

!!! warning "Experimental"
    New in 0.7: its API may still change in any release, as its use shows what's missing.
    [Scope and stability](../features/scope.md) lists the tools that are settled.

Passwords from sign-up to reset: new ones refused when they're easy to guess, common or leaked,
kept as slow hashes, guessed at most a few times per account, and reset by a link that works
once. `FastAPIPasswords`, with nothing to install: the hashes are scrypt's, from the standard
library. Two checks of new passwords are optional, each with its extra.

Every class and function: [the API reference](../reference/auth.md#fastapi_alcazaba.auth.FastAPIPasswords).
In an app: [the example](../examples/passwords.md).

```console
$ pip install "fastapi-alcazaba[passwords]"           # PwnedPasswords: httpx2
$ pip install "fastapi-alcazaba[password-strength]"   # PasswordStrength: zxcvbn
```

```python
from fastapi_alcazaba import FastAPIPasswords
from fastapi_alcazaba.auth import PasswordBlocklist, PasswordStrength, PwnedPasswords

passwords = FastAPIPasswords(
    pepper=settings.password_pepper,  # a SecretStr of 32 bytes or more
    strength=PasswordStrength(min_score=3),
    blocklist=PasswordBlocklist.from_files("banned.txt"),
    pwned=PwnedPasswords(),
    audit=audit,
)
```

## New passwords

`evaluate` says what's wrong with a password at sign-up, at a change and at a reset, as NIST SP
800-63B says, and what it checked:

```python
@app.post("/signup", status_code=201)
async def signup(form: SignUp) -> Account:
    password = form.password.get_secret_value()  # a SecretStr: out of reprs and logs
    result = await passwords.evaluate(password, user_inputs=[form.email, form.name])
    if not result.ok:
        detail = {"password": list(result.problems), "suggestions": list(result.suggestions)}
        raise HTTPException(422, detail)
    user = await db.users.create(email=form.email, password=await passwords.hash(password))
    ...
```

With nothing more, it refuses fewer than `min_length` characters (15 by default, the length for
a password that is the only factor; 8 with a second factor), more than 1024, and the user's data
in it (`user_inputs`: the e-mail address or its local part, the name, and their words, of 4
characters or more, whatever their case and accents: `zurbaran84` for Belén Zurbarán).
No rules on its characters (a digit, a capital, a symbol): length is what makes a password
strong. Each check below is off until it's passed to `FastAPIPasswords`.

`check()` gives the same problems as a list, for a quick `if problems := await
passwords.check(...)`.

### The result

```pycon
>>> await passwords.evaluate("qwertyuiopasdfghjkl", user_inputs=["ana@example.com"])
PasswordCheck(problems=('weak',), score=1, checks=('length', 'characters', 'user_inputs', 'blocklist', 'strength'), unavailable=(), suggestions=('This is similar to a commonly used password.', 'Add another word or two. Uncommon words are better.'))
```

| Field | What it says |
| --- | --- |
| `ok` | whether the password is accepted: no problems |
| `problems` | why it's refused, as codes for the app to translate (below) |
| `score` | zxcvbn's, 0 to 4; `None` without `strength` |
| `checks` | the checks made |
| `unavailable` | the checks that couldn't be made: `("pwned",)` when Have I Been Pwned didn't answer and the check fails open |
| `suggestions` | zxcvbn's advice for a better one, in English, for weak ones |

| Problem | The password |
| --- | --- |
| `too_short` | has fewer than `min_length` characters |
| `too_long` | has more than 1024: never hashed |
| `invalid_characters` | has a lone surrogate (`"\ud800"`), which JSON lets through but no text holds |
| `contains_user_input` | holds one of `user_inputs`, or one of their words |
| `blocklisted` | is in the app's blocklist |
| `weak` | scores below `min_score` |
| `pwned` | is among those leaked; asked only when there's no other problem |

The result never holds the password, so it can be logged or returned as it is.

### Strength

`PasswordStrength` measures how many guesses a password would take with
[zxcvbn](https://github.com/dwolfhub/zxcvbn-python), Dropbox's estimator: it finds dictionary
words, names, the 30 000 most common passwords, keyboard walks (`qwertyuiop`), dates, repeats,
sequences and l33t (`p@ssw0rd`), and scores the guesses from 0 to 4:

| `min_score` | zxcvbn's guesses | For |
| --- | --- | --- |
| 2 | 10^8: "somewhat guessable" | the least, with a second factor |
| 3 (default) | 10^10: "safely unguessable" | most accounts |
| 4 | more: "very unguessable" | administrators, money |

- **The user's data** in `user_inputs` count as words an attacker knows: their words (a name's,
  an e-mail address's local part and domain) and without accents too, so that
  `zurbaranbelen` is weak for Belén Zurbarán.
- **72 characters** at most are measured (zxcvbn's limit): a longer password has its first 72
  scored.
- **In a thread:** zxcvbn takes a millisecond or so, up to some tens for long repetitive
  passwords; `evaluate` runs it in a thread, one call at a time (zxcvbn keeps the user's data
  of a call in a module global). `strength.measure(password)` is the same, synchronous, for a
  Pydantic validator: it blocks the event loop meanwhile.

### Blocklist

`PasswordBlocklist` refuses the app's own words, which no list of common passwords has (its name,
its products, its city), or a list of common ones:

```python
blocklist = PasswordBlocklist(["Lighthouse", "Lighthouse Notes", "lighthousenotes"])
blocklist = PasswordBlocklist.from_files("common-100k.txt", "ours.txt")  # one per line
```

- **Loaded once,** when it's built, at start-up: a `frozenset`, then each check is a lookup.
  About a hundred bytes per entry: SecLists' or NCSC's top 100 000 take some 10 MB. Lists of
  millions are Have I Been Pwned's job.
- **Normalized** to compare (NFKC, casefolded, without spaces around): `Lighthouse`,
  `LIGHTHOUSE ` and `ＬＩＧＨＴＨＯＵＳＥ` are the same. The password that's hashed is the one
  sent.
- **Whole passwords** only: `lighthouse2024` isn't `lighthouse`. Variations are zxcvbn's job,
  with the same words in `user_inputs` if they matter.
- A file that isn't UTF-8 raises at start-up: give its `encoding` (`"latin-1"` for RockYou).

### Leaked passwords

Leaked passwords are the first that attackers try. `PwnedPasswords` asks
[Have I Been Pwned](https://haveibeenpwned.com/Passwords) without sending the password: only the
first five characters of its SHA-1 go out (k-anonymity), and the answer, the suffixes of every
leaked hash with that prefix, is padded, so that its size doesn't tell them either. The match is
made in the app.

```python
pwned = PwnedPasswords(timeout=2.0, fail_open=True)
```

When the API doesn't answer within `timeout`, answers an error, or answers something that isn't
a range answer (a proxy's login page, a count of 5000 digits), the check is unavailable:

| `fail_open` | When unavailable | For |
| --- | --- | --- |
| `True` (default) | the password is accepted; `result.unavailable == ("pwned",)`, and a warning logged | most apps: a sign-up mustn't wait for a third party; check again at the next login if it matters |
| `False` | `BackendUnavailable` is raised: answer a 503 | apps that would rather refuse than accept a password unchecked |

```python
from fastapi_alcazaba.core.errors import BackendUnavailable


@app.exception_handler(BackendUnavailable)
async def unavailable(request: Request, exc: BackendUnavailable) -> JSONResponse:
    return JSONResponse({"detail": "Try again in a moment"}, status_code=503)
```

Without `pwned`, no call is made. The warning and the exception hold the error's class only,
never the password, its hash or the prefix.

!!! warning "httpx2 logs the prefix"

    httpx2 logs each request at `INFO`, with its URL, and so the prefix of the password's hash:
    20 of its 160 bits, which narrow an offline guess. Keep the `httpx2` logger at `WARNING`
    (`logging.getLogger("httpx2").setLevel(logging.WARNING)`), as Python's default is.

## Changing a password

The current password first, then the new one, evaluated as at sign-up:

```python
@app.post("/password/change", status_code=204)
async def change_password(request: Request, form: PasswordChange) -> None:
    user = await current_user(request)
    current = form.current_password.get_secret_value()
    if not await passwords.verify(user.id, current, user.password, request=request):
        raise HTTPException(401, "Wrong password")
    new = form.new_password.get_secret_value()
    result = await passwords.evaluate(new, user_inputs=[user.email, user.name])
    if not result.ok:
        raise HTTPException(422, {"password": list(result.problems)})
    await db.users.update(user.id, password=await passwords.hash(new))
    await user_sessions.end_all(user.id, request=request, keep_current=True)  # the others are out
```

`verify` counts the wrong current passwords as it does at login: someone with a stolen session
can't use the form to guess.

## Hashes

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
