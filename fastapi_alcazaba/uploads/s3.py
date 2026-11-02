"""Files in S3 (or anything that speaks its API: MinIO, Cloudflare R2, Google Cloud Storage's
interoperability, Backblaze B2...) without passing through the app: URLs signed with AWS's
Signature Version 4, which the browser uses directly. Needs nothing more.

    s3 = S3Presigner(bucket="uploads", region="eu-west-1",
                     access_key=settings.s3_access_key, secret_key=settings.s3_secret_key)

    form = s3.upload_form(f"avatars/{user.id}", max_size=2_000_000, content_type_prefix="image/")
    # the browser posts the file to form["url"] with form["fields"]: S3 refuses one too large

    url = s3.download_url(f"invoices/{invoice.id}.pdf", filename="invoice.pdf", expires=60)

The bucket stays private: each URL is for one object, one method, and a few minutes. Check what
was uploaded before using it (`FastAPIUploads`, reading it from the bucket): the form bounds its
size and its declared type, not what it is.
"""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import SecretStr

#: The longest a signed URL may last: Signature Version 4's limit, 7 days.
MAX_EXPIRES = 7 * 24 * 3600
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]")
_REGION = re.compile(r"[a-z0-9\-]{2,32}")
_UNSIGNED = "UNSIGNED-PAYLOAD"


def _encode(text: str, safe: str = "-_.~") -> str:
    """RFC 3986, as Signature Version 4 encodes: everything but the unreserved characters."""
    return quote(text, safe=safe)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def _check_key(key: str) -> str:
    """An object key that can't escape its prefix nor confuse a path: no leading slash, no `.`
    or `..` segment, no control characters, 1024 bytes of UTF-8 at most."""
    if not isinstance(key, str) or not key or len(key.encode()) > 1024 or key.startswith("/"):
        raise ValueError("an object key is 1 to 1024 bytes, not starting with /")
    if any(segment in (".", "..", "") for segment in key.split("/")[:-1] + [key.split("/")[-1]]):
        raise ValueError(f"an object key has no empty, . or .. segment: {key!r}")
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise ValueError("an object key has no control characters")
    return key


@dataclass(frozen=True, slots=True)
class UploadForm:
    """What the browser posts the file with: `fields` first, then the file as `file`.

    :param url: where to post it.
    :param fields: the form's fields, the signed policy included.
    """

    url: str
    fields: dict[str, str]


class S3Presigner:
    """Signs URLs and upload forms for one bucket (Signature Version 4, query or POST policy).

    :param bucket: the bucket's name.
    :param region: its region (`us-east-1`, `auto` for R2...).
    :param access_key: the access key's ID, of a user that may only do what the URLs need on this
        bucket.
    :param secret_key: its secret (a SecretStr from your settings).
    :param session_token: the token of temporary credentials (STS), signed into each URL.
    :param endpoint: the service's URL, for other than AWS
        (`https://<account>.r2.cloudflarestorage.com`, `http://localhost:9000` for MinIO); AWS's
        for the region by default.
    :param path_style: the bucket in the path (`endpoint/bucket/key`) rather than in the host:
        what MinIO and most others want; AWS takes virtual hosts.
    :param service: the signing service's name: `s3`.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str | SecretStr,
        session_token: str | SecretStr | None = None,
        endpoint: str | None = None,
        path_style: bool | None = None,
        service: str = "s3",
    ) -> None:
        if not _BUCKET.fullmatch(bucket) or ".." in bucket:
            raise ValueError(f"not a bucket name: {bucket!r}")
        if not _REGION.fullmatch(region):
            raise ValueError(f"not a region: {region!r}")
        if not access_key:
            raise ValueError("give the access key's ID")
        secret = secret_key if isinstance(secret_key, SecretStr) else SecretStr(secret_key)
        if not secret.get_secret_value():
            raise ValueError("the secret key is empty")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self._secret = secret
        token = session_token
        self._token = SecretStr(token) if isinstance(token, str) else token
        self.service = service
        self.path_style = endpoint is not None if path_style is None else path_style
        if endpoint is None:
            endpoint = f"https://s3.{region}.amazonaws.com"
        parts = urlsplit(endpoint)
        local = parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
        bare = parts.netloc and parts.path in ("", "/")
        if not (parts.scheme == "https" or local) or not bare:
            raise ValueError(f"the endpoint is https://host[:port] or http://localhost: {endpoint}")
        self.scheme = parts.scheme
        self.host = parts.netloc if self.path_style else f"{bucket}.{parts.netloc}"

    # URLs.

    def download_url(
        self,
        key: str,
        *,
        expires: int = 300,
        filename: str | None = None,
        content_type: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """A URL that GETs the object `key` for `expires` seconds. With `filename`, browsers
        download it as that file (`Content-Disposition: attachment`) rather than show it, so an
        uploaded HTML or SVG never runs on the bucket's origin; `content_type` overrides its
        type."""
        query = {}
        if filename is not None:
            if any(ord(char) < 32 or char in '"\\' for char in filename):
                raise ValueError("a filename has no quotes, backslashes nor control characters")
            fallback = "".join(char if 32 <= ord(char) < 127 else "_" for char in filename)
            query["response-content-disposition"] = (
                f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{_encode(filename)}"
            )
        if content_type is not None:
            query["response-content-type"] = content_type
        return self._presign("GET", key, expires, query, now)

    def upload_url(
        self,
        key: str,
        *,
        expires: int = 300,
        now: datetime | None = None,
    ) -> str:
        """A URL that PUTs the object `key` for `expires` seconds, whatever its size: prefer
        `upload_form`, which bounds it."""
        return self._presign("PUT", key, expires, {}, now)

    def upload_form(
        self,
        key: str,
        *,
        max_size: int,
        min_size: int = 1,
        content_type: str | None = None,
        content_type_prefix: str | None = None,
        expires: int = 300,
        now: datetime | None = None,
    ) -> UploadForm:
        """A form that POSTs one file as the object `key` for `expires` seconds, refused by the
        service when it's smaller than `min_size` or larger than `max_size` bytes, or declares
        another type than `content_type` (or one that doesn't start with
        `content_type_prefix`, such as "image/"). Overwrites what's under `key`: give each
        upload a key of its own."""
        _check_key(key)
        if not 0 <= min_size <= max_size:
            raise ValueError("min_size and max_size are 0 <= min_size <= max_size")
        if content_type is not None and content_type_prefix is not None:
            raise ValueError("give content_type or content_type_prefix, not both")
        now = self._now(now)
        expires = self._expires(expires)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        credential = f"{self.access_key}/{self._scope(day)}"
        fields: dict[str, str] = {
            "key": key,
            "x-amz-algorithm": "AWS4-HMAC-SHA256",
            "x-amz-credential": credential,
            "x-amz-date": stamp,
        }
        if self._token is not None:
            fields["x-amz-security-token"] = self._token.get_secret_value()
        conditions: list[Any] = [{"bucket": self.bucket}, ["eq", "$key", key]]
        conditions += [{name: value} for name, value in fields.items() if name != "key"]
        conditions.append(["content-length-range", min_size, max_size])
        if content_type is not None:
            fields["Content-Type"] = content_type
            conditions.append(["eq", "$Content-Type", content_type])
        elif content_type_prefix is not None:
            conditions.append(["starts-with", "$Content-Type", content_type_prefix])
        policy = {
            "expiration": (now + timedelta(seconds=expires)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "conditions": conditions,
        }
        encoded = base64.b64encode(json.dumps(policy, separators=(",", ":")).encode()).decode()
        fields["policy"] = encoded
        fields["x-amz-signature"] = hmac.new(
            self._signing_key(day), encoded.encode(), hashlib.sha256
        ).hexdigest()
        return UploadForm(url=self._bucket_url(), fields=fields)

    # Signature Version 4.

    def _presign(
        self,
        method: str,
        key: str,
        expires: int,
        extra: dict[str, str],
        now: datetime | None,
    ) -> str:
        _check_key(key)
        now = self._now(now)
        stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{self._scope(day)}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(self._expires(expires)),
            "X-Amz-SignedHeaders": "host",
            **extra,
        }
        if self._token is not None:
            query["X-Amz-Security-Token"] = self._token.get_secret_value()
        path = self._path(key)
        canonical_query = "&".join(
            f"{_encode(name)}={_encode(value)}" for name, value in sorted(query.items())
        )
        canonical = "\n".join(
            [method, path, canonical_query, f"host:{self.host}", "", "host", _UNSIGNED]
        )
        to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                stamp,
                self._scope(day),
                hashlib.sha256(canonical.encode()).hexdigest(),
            ]
        )
        signature = hmac.new(self._signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest()
        return f"{self.scheme}://{self.host}{path}?{canonical_query}&X-Amz-Signature={signature}"

    def _path(self, key: str) -> str:
        encoded = _encode(key, safe="-_.~/")
        return f"/{self.bucket}/{encoded}" if self.path_style else f"/{encoded}"

    def _bucket_url(self) -> str:
        return f"{self.scheme}://{self.host}/" + (f"{self.bucket}/" if self.path_style else "")

    def _scope(self, day: str) -> str:
        return f"{day}/{self.region}/{self.service}/aws4_request"

    def _signing_key(self, day: str) -> bytes:
        key = _hmac(f"AWS4{self._secret.get_secret_value()}".encode(), day)
        for part in (self.region, self.service, "aws4_request"):
            key = _hmac(key, part)
        return key

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now is an aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _expires(expires: int) -> int:
        if not 1 <= expires <= MAX_EXPIRES:
            raise ValueError(f"expires is 1 to {MAX_EXPIRES} seconds")
        return expires
