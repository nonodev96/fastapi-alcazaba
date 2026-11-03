"""Signed webhooks of every scheme (Standard Webhooks, GitHub, Stripe, Slack): whatever headers
and body arrive, verifying them gives a 400 or a 401 unless they were signed with the secret; and
what the secret signs is always accepted."""

import base64
import hashlib
import hmac
import sys
import time
from contextlib import nullcontext

from fastapi import HTTPException

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

# Only Alcazaba's code is instrumented: its coverage is what guides the fuzzer, and instrumenting
# every library imported takes longer than a short run lasts.
with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookSigner

_SECRET = "whsec_" + base64.b64encode(b"fuzz-only-webhook-secret-32bytes").decode()
_PROVIDER_SECRET = "fuzz-only-provider-secret"  # noqa: S105  # the fuzzer's own
_SCHEMES = {
    "standard": (
        FastAPIWebhooks(_SECRET),
        ("webhook-id", "webhook-timestamp", "webhook-signature"),
    ),
    "github": (
        FastAPIWebhooks(_PROVIDER_SECRET, scheme="github"),
        ("x-github-delivery", "x-hub-signature-256"),
    ),
    "stripe": (FastAPIWebhooks(_PROVIDER_SECRET, scheme="stripe"), ("stripe-signature",)),
    "slack": (
        FastAPIWebhooks(_PROVIDER_SECRET, scheme="slack"),
        ("x-slack-request-timestamp", "x-slack-signature"),
    ),
}
_NAMES = sorted(_SCHEMES)
_SIGNER = WebhookSigner(_SECRET)


def _signed(scheme: str, body: bytes) -> dict[str, str]:
    """Headers that the sender of `scheme` would send with `body`, signed now."""
    now = str(int(time.time()))
    key = _PROVIDER_SECRET.encode()
    if scheme == "standard":
        return _SIGNER.sign(body)
    if scheme == "github":
        mac = hmac.new(key, body, hashlib.sha256).hexdigest()
        return {"x-github-delivery": "d1", "x-hub-signature-256": f"sha256={mac}"}
    if scheme == "stripe":
        mac = hmac.new(key, f"{now}.".encode() + body, hashlib.sha256).hexdigest()
        return {"stripe-signature": f"t={now},v1={mac}"}
    mac = hmac.new(key, f"v0:{now}:".encode() + body, hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": now, "x-slack-signature": f"v0={mac}"}


def fuzz_one_input(data: bytes) -> None:
    # The first byte picks the scheme; then header values as Starlette gives them (latin-1), and
    # the body; a header may be missing.
    if not data:
        return
    scheme = _NAMES[data[0] % len(_NAMES)]
    webhooks, names = _SCHEMES[scheme]
    *values, body = data[1:].split(b"\n", len(names))
    headers = {name: value.decode("latin-1") for name, value in zip(names, values, strict=False)}
    try:
        webhooks.verify(headers, body)
    except HTTPException as exc:
        assert exc.status_code in (400, 401), exc.status_code
    else:
        raise AssertionError(f"accepted without the secret's signature: {scheme} {headers!r}")

    assert webhooks.verify(_signed(scheme, body), body).body == body


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
