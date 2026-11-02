# Passkeys

--8<-- "docs/examples/animations/passkeys.svg"

A site without passwords: people sign up and log in with a passkey, see the devices they're
logged in on, and log them out. [`FastAPIWebAuthn`](../guide/passkeys.md) checks the passkeys;
[`UserSessions`](../guide/browser.md#each-users-sessions) keeps each user's sessions.

## Run it

From a clone of the repository:

```bash
make deps                 # .venv, with every extra
source .venv/bin/activate
uvicorn examples.passkeys.main:app
```

Open <http://localhost:8000> in a browser with passkeys (Chrome, Safari, Edge, Firefox). Set
`CACHE_SIGNING_KEY` (`secrets.token_urlsafe(32)`), the same in every process; `WEBAUTHN_RP_ID` and
`WEBAUTHN_ORIGIN` for another site than `localhost:8000`.

## Try it

The page signs up with a username and a passkey (Touch ID, Windows Hello, a phone, a security
key), then logs in with it: the button asks the browser for any passkey of the site, without a
username. Each ceremony is two requests: the options, then the browser's answer. The options of
a login:

```console
$ curl -X POST http://localhost:8000/login/options -H "Sec-Fetch-Site: same-origin"
{"challenge":"rMssb7FOjtQJeW6QJkPiLJUDKmRDA7TNmpxGcLay-c4","rpId":"localhost","timeout":300000,"allowCredentials":[],"userVerification":"required"}
```

Ana signed up on her laptop, and logged in with her passkey on her phone and on a PC. Her
laptop lists the three devices (what follows was answered by the app to a software authenticator,
as the browser's):

```json
[
  {"ref":"57b2bbaf20e3bbab6cbfa1281b84b05e","since":"2026-11-01T23:53:34+00:00","last_seen":"2026-11-01T23:53:34+00:00","device":"Mozilla/5.0 (Macintosh) Safari/18.0","this_one":true},
  {"ref":"ea719ae49a0a29889b6a202b2baeeca9","since":"2026-11-01T23:53:34+00:00","last_seen":"2026-11-01T23:53:34+00:00","device":"Mozilla/5.0 (iPhone) Mobile Safari/18.0","this_one":false},
  {"ref":"e510d3103080e948283944855ca5cf20","since":"2026-11-01T23:53:34+00:00","last_seen":"2026-11-01T23:53:34+00:00","device":"Mozilla/5.0 (Windows NT 10.0) Chrome/130.0","this_one":false}
]
```

`DELETE /sessions/ea719ae49a0a29889b6a202b2baeeca9` logs the phone out (a 204); its next request
is a 401. `POST /sessions/end-all` logs out every other device:

```json
{"ended":1}
```

and the laptop is the only device left, with a new session id.

## What to look at

- **No password to steal:** the database keeps public keys, and each login is a signature over a
  challenge of the site, for its origin: a phishing page gets nothing it can use.
- **Without a username:** the passkey says whose it is (its user handle); the app looks it up by
  its id, and verifies its signature and counter.
- **Both factors:** the authenticator verified the user (a fingerprint, a PIN), so a login is a
  recent second factor: deleting the account needs one of the last five minutes.
- **Each user's sessions:** `UserSessions` lists them with when they started and the browser they
  came from; one ends at once, and `end_all` refuses every session of the user that started
  before, even one the index missed.

## The code

<!-- fmt: off -->
```python title="examples/passkeys/main.py"
--8<-- "examples/passkeys/main.py"
```
<!-- fmt: on -->
