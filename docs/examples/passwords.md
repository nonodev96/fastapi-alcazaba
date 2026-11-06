# Passwords

--8<-- "docs/examples/animations/passwords.svg"

Accounts with a password, for a notes app called Lighthouse Notes: at sign-up and at every
change, [`FastAPIPasswords.evaluate`](../guide/passwords.md#new-passwords) refuses passwords
that are short, made of the person's data, the app's own words, easy to guess for zxcvbn, or
among those leaked, and says why.

Ana Belén Zurbarán signs up, tries a few passwords until one is accepted, logs in, and changes
it.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra (passwords and password-strength among them)
source .venv/bin/activate
uvicorn examples.passwords.main:app
```

Set `CACHE_SIGNING_KEY` and `PASSWORD_PEPPER` (`secrets.token_urlsafe(32)` each), the same in
every process. The leak check asks Have I Been Pwned: the app needs to reach
`api.pwnedpasswords.com`, and when it can't, accepts the password (`fail_open`).

## Try it

Each refusal is a 422 whose `password` lists the problems, as codes for the app to translate,
and whose `suggestions` are zxcvbn's advice. Too short, and only a word:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "short"}'
{"detail":{"password":["too_short","weak"],"suggestions":["A word by itself is easy to guess.","Add another word or two. Uncommon words are better."]}}
```

Her own name, written without its accents, and a year:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "zurbaranbelen1984"}'
{"detail":{"password":["contains_user_input"],"suggestions":[]}}
```

Long, but a walk along the keyboard:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "qwertyuiopasdfghjkl"}'
{"detail":{"password":["weak"],"suggestions":["This is similar to a commonly used password.","Add another word or two. Uncommon words are better."]}}
```

The app's name, in its blocklist, whatever the case:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "LIGHTHOUSE NOTES"}'
{"detail":{"password":["blocklisted"],"suggestions":[]}}
```

Strong for zxcvbn, but famous, and so among the leaked passwords. Only `ABF7A`, the first five
characters of its SHA-1, went to Have I Been Pwned:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "correct horse battery staple"}'
{"detail":{"password":["pwned"],"suggestions":[]}}
```

Four words of her own choosing:

```console
$ curl -X POST http://localhost:8000/signup -H "Content-Type: application/json" -d '{"email": "ana@example.com", "name": "Ana Belén Zurbarán", "password": "sunrise velvet harbour lantern"}'
{"id":1,"email":"ana@example.com","name":"Ana Belén Zurbarán"}

$ curl -X POST http://localhost:8000/login -H "Content-Type: application/json" -d '{"email": "ana@example.com", "password": "sunrise velvet harbour lantern"}'
{"id":1,"email":"ana@example.com","name":"Ana Belén Zurbarán"}
```

A change takes the current password, then evaluates the new one as at sign-up. Her name in l33t
and repeated isn't enough:

```console
$ curl -X POST http://localhost:8000/password/change -H "Content-Type: application/json" -d '{"email": "ana@example.com", "current_password": "sunrise velvet harbour lantern", "new_password": "zurb4r4nzurb4r4n1984"}'
{"detail":{"password":["weak"],"suggestions":["Repeats like \"abcabcabc\" are only slightly harder to guess than \"abc\".","Add another word or two. Uncommon words are better.","Avoid repeated words and characters."]}}

$ curl -i -X POST http://localhost:8000/password/change -H "Content-Type: application/json" -d '{"email": "ana@example.com", "current_password": "sunrise velvet harbour lantern", "new_password": "quiet orchard copper meadow"}'
HTTP/1.1 204 No Content
```

## How it works

- **One `FastAPIPasswords`** with the three checks: `PasswordStrength(min_score=3)`, a
  `PasswordBlocklist` of the app's words, and `PwnedPasswords(timeout=2.0, fail_open=True)`.
- **`refuse_if_weak`** evaluates a new password with the person's e-mail address and name, and
  answers the problems and the advice in a 422.
- **The passwords are `SecretStr`** in the request models, so that they stay out of reprs, logs
  and tracebacks; `get_secret_value()` gives them to `evaluate`, `hash` and `verify`.
- **When Have I Been Pwned doesn't answer,** the password is accepted and a warning logged. With
  `fail_open=False`, `BackendUnavailable` is raised instead, and the app's handler answers a 503.
- **A wrong current password** at a change counts against the account like one at login.

## The code

<!-- fmt: off -->
```python title="examples/passwords/main.py"
--8<-- "examples/passwords/main.py"
```
<!-- fmt: on -->
