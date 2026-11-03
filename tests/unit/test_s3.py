"""S3Presigner: Signature Version 4 as AWS documents it (its example's signature), and the URLs
and forms of each method, bounded."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from pydantic import SecretStr

from fastapi_alcazaba.uploads.s3 import MAX_EXPIRES, S3Presigner

# AWS's documented example: "Authenticating Requests: Using Query Parameters (AWS Signature
# Version 4)", GET examplebucket/test.txt on 24 May 2013, for a day.
EXAMPLE_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105  # AWS's documented example
EXAMPLE_SIGNATURE = "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
NOW = datetime(2013, 5, 24, tzinfo=UTC)


def example(**options: Any) -> S3Presigner:
    return S3Presigner(
        **{
            "bucket": "examplebucket",
            "region": "us-east-1",
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": EXAMPLE_KEY,
            "endpoint": "https://s3.amazonaws.com",
            "path_style": False,
            **options,
        }
    )


def query(url: str) -> dict[str, str]:
    return {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}


def test_awss_documented_example() -> None:
    url = example().download_url("test.txt", expires=86400, now=NOW)
    assert url == (
        "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        f"&X-Amz-Signature={EXAMPLE_SIGNATURE}"
    )


def test_a_download_as_a_file() -> None:
    url = example().download_url("invoices/7.pdf", filename="factura ñ.pdf", now=NOW)
    found = query(url)
    assert found["response-content-disposition"] == (
        "attachment; filename=\"factura _.pdf\"; filename*=UTF-8''factura%20%C3%B1.pdf"
    )
    assert found["X-Amz-Expires"] == "300"
    with pytest.raises(ValueError, match="quotes"):
        example().download_url("x", filename='a"b')


def test_keys_are_encoded_once() -> None:
    url = example().download_url("a folder/ñ+=.txt", now=NOW)
    assert urlsplit(url).path == "/a%20folder/%C3%B1%2B%3D.txt"
    assert unquote(urlsplit(url).path) == "/a folder/ñ+=.txt"


def test_path_style_for_other_services() -> None:
    minio = S3Presigner(
        bucket="uploads",
        region="us-east-1",
        access_key="minio",
        secret_key=SecretStr("secret-of-minio"),
        endpoint="http://localhost:9000",
    )
    url = minio.upload_url("a.txt", size=3, now=NOW).url
    assert url.startswith("http://localhost:9000/uploads/a.txt?")
    assert minio.upload_form("a.txt", max_size=10, now=NOW).url == "http://localhost:9000/uploads/"


def test_temporary_credentials() -> None:
    url = example(session_token="the-sts-token").download_url("test.txt", now=NOW)  # noqa: S106
    assert query(url)["X-Amz-Security-Token"] == "the-sts-token"


def test_an_upload_form() -> None:
    s3 = example()
    form = s3.upload_form(
        "avatars/u1", max_size=2_000_000, content_type_prefix="image/", expires=600, now=NOW
    )
    assert form.url == "https://examplebucket.s3.amazonaws.com/"
    policy = json.loads(base64.b64decode(form.fields["policy"]))
    assert policy["expiration"] == "2013-05-24T00:10:00.000Z"
    assert {"bucket": "examplebucket"} in policy["conditions"]
    assert ["eq", "$key", "avatars/u1"] in policy["conditions"]
    assert ["content-length-range", 1, 2_000_000] in policy["conditions"]
    assert ["starts-with", "$Content-Type", "image/"] in policy["conditions"]
    assert {"x-amz-date": "20130524T000000Z"} in policy["conditions"]
    # The signature is the policy's, with the day's signing key.
    key = f"AWS4{EXAMPLE_KEY}".encode()
    for part in ("20130524", "us-east-1", "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, form.fields["policy"].encode(), hashlib.sha256).hexdigest()
    assert form.fields["x-amz-signature"] == expected


def test_a_form_for_one_type() -> None:
    form = example().upload_form("doc.pdf", max_size=10, content_type="application/pdf", now=NOW)
    policy = json.loads(base64.b64decode(form.fields["policy"]))
    assert form.fields["Content-Type"] == "application/pdf"
    assert ["eq", "$Content-Type", "application/pdf"] in policy["conditions"]


@pytest.mark.parametrize(
    "key",
    ["", "/absolute", "a/../b", "a/./b", "a//b", "trailing/", "..", "a\nb", "x" * 1025, "ñ" * 513],
)
def test_keys_that_arent_accepted(key: str) -> None:
    with pytest.raises(ValueError, match="object key"):
        example().download_url(key)


@pytest.mark.parametrize("expires", [0, MAX_EXPIRES + 1])
def test_expires(expires: int) -> None:
    with pytest.raises(ValueError, match="expires"):
        example().download_url("a", expires=expires)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"bucket": "Bad_Bucket"}, "bucket"),
        ({"bucket": "a..b"}, "bucket"),
        ({"region": "EU West"}, "region"),
        ({"access_key": ""}, "access key"),
        ({"secret_key": ""}, "secret key"),
        ({"endpoint": "http://s3.example.com"}, "endpoint"),
        ({"endpoint": "https://s3.example.com/path"}, "endpoint"),
    ],
)
def test_configuration(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        example(**options)


def test_form_bounds() -> None:
    with pytest.raises(ValueError, match="min_size"):
        example().upload_form("a", max_size=1, min_size=2)
    with pytest.raises(ValueError, match="not both"):
        example().upload_form("a", max_size=1, content_type="a/b", content_type_prefix="a/")
    with pytest.raises(ValueError, match="aware"):
        example().upload_url("a", size=1, now=datetime(2013, 5, 24))  # noqa: DTZ001


def test_the_secret_isnt_in_the_url() -> None:
    url = example().download_url("test.txt")
    assert EXAMPLE_KEY not in url
    assert "EXAMPLEKEY" not in repr(example().__dict__)
