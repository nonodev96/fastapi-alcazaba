"""Signed webhooks: whatever headers and body arrive, verifying them gives a 400 or a 401 unless
they were signed with the secret; and what the secret signs is always accepted."""

import base64
import sys

from fastapi import HTTPException

from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookSigner

_SECRET = "whsec_" + base64.b64encode(b"fuzz-only-webhook-secret-32bytes").decode()
_WEBHOOKS = FastAPIWebhooks(_SECRET)
_SIGNER = WebhookSigner(_SECRET)
_HEADERS = ("webhook-id", "webhook-timestamp", "webhook-signature")


def fuzz_one_input(data: bytes) -> None:
    # Header values as Starlette gives them (latin-1), then the body; a header may be missing.
    *values, body = data.split(b"\n", len(_HEADERS))
    headers = {name: value.decode("latin-1") for name, value in zip(_HEADERS, values, strict=False)}
    try:
        _WEBHOOKS.verify(headers, body)
    except HTTPException as exc:
        assert exc.status_code in (400, 401), exc.status_code
    else:
        raise AssertionError(f"accepted without the secret's signature: {headers!r}")

    signed = _SIGNER.sign(body)
    assert _WEBHOOKS.verify(signed, body).body == body


def main() -> None:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image

    atheris.instrument_all()
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
