"""The cookie helpers of the session and CSRF middlewares."""

from typing import Any

import pytest

from fastapi_alcazaba._cookies import CookieSettings, cookie_values, has_cookies


class TestSettings:
    def test_secure_defaults(self) -> None:
        assert CookieSettings("session").header("abc", 60) == (
            b"set-cookie",
            b"session=abc; Max-Age=60; Path=/; HttpOnly; Secure; SameSite=Lax",
        )

    def test_every_attribute(self) -> None:
        cookie = CookieSettings(
            "id", secure=False, same_site="strict", http_only=False, path="/app", domain="a.b"
        )
        assert cookie.header("x") == (
            b"set-cookie",
            b"id=x; Path=/app; Domain=a.b; SameSite=Strict",
        )

    def test_expired(self) -> None:
        assert CookieSettings("__Host-s", same_site="none").expired() == (
            b"set-cookie",
            b"__Host-s=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/; HttpOnly; "
            b"Secure; SameSite=None",
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"name": "a b"}, "invalid cookie name"),
            ({"name": "a;b"}, "invalid cookie name"),
            ({"same_site": "loose"}, "same_site is"),
            ({"same_site": "none", "secure": False}, "SameSite=None"),
            ({"path": "app"}, "invalid cookie path"),
            ({"path": "/a;b"}, "invalid cookie path"),
            ({"domain": "a b.com"}, "invalid cookie domain"),
            ({"domain": "-a.com"}, "invalid cookie domain"),
            ({"name": "__Secure-id", "secure": False}, "must be Secure"),
            ({"name": "__host-id", "secure": False}, "must be Secure"),
            ({"name": "__Host-id", "path": "/app"}, "path /"),
            ({"name": "__Host-id", "domain": "example.com"}, "no domain"),
        ],
    )
    def test_refused(self, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            CookieSettings(**{"name": "id", **options})

    def test_prefixes_with_what_they_need(self) -> None:
        CookieSettings("__Secure-id", domain=".example.com", path="/app")
        CookieSettings("__Host-id")


class TestReading:
    def test_values_from_every_cookie_header(self) -> None:
        scope = {
            "headers": [
                (b"cookie", b"a=1; session=first ;b"),
                (b"accept", b"session=no"),
                (b"cookie", b"session=second; sessions=no"),
            ]
        }
        assert cookie_values(scope, "session") == ["first", "second"]
        assert cookie_values(scope, "missing") == []

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [([], False), ([(b"cookie", b"  ")], False), ([(b"cookie", b"a=1")], True)],
    )
    def test_has_cookies(self, headers: list[tuple[bytes, bytes]], expected: bool) -> None:
        assert has_cookies({"headers": headers}) is expected
