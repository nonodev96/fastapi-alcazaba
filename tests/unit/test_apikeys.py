"""API keys: generated once, stored as digests, checked in constant time, scopes and expiry."""

import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, assert_type

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from fastapi_alcazaba.apikeys import APIKeyRecord, FastAPIAPIKeys, NewAPIKey

PEPPER = "p" * 32
OLD_PEPPER = "o" * 32
KEY_PATTERN = re.compile(r"ak_[a-z2-7]{16}_[a-z2-7]{52}")


class Store:
    """The app's table of keys, looked up by id."""

    def __init__(self) -> None:
        self.records: dict[str, APIKeyRecord] = {}
        self.lookups: list[str] = []

    def add(self, new: NewAPIKey) -> str:
        self.records[new.record.id] = new.record
        return new.key

    async def lookup(self, key_id: str) -> APIKeyRecord | None:
        self.lookups.append(key_id)
        return self.records.get(key_id)

    def lookup_sync(self, key_id: str) -> APIKeyRecord | None:
        self.lookups.append(key_id)
        return self.records.get(key_id)


class Clock:
    """Stands in for the `time` module of fastapi_alcazaba.apikeys (the cache's clock)."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(name="store")
def fixture_store() -> Store:
    return Store()


@pytest.fixture(name="keys")
def fixture_keys(store: Store) -> FastAPIAPIKeys[APIKeyRecord]:
    return FastAPIAPIKeys(store.lookup)


async def refused(keys: FastAPIAPIKeys[Any], key: str | None) -> HTTPException:
    with pytest.raises(HTTPException) as error:
        await keys.authenticate(key)
    return error.value


class TestGenerate:
    def test_a_new_key_and_its_record(self, keys: FastAPIAPIKeys[APIKeyRecord]) -> None:
        expiry = datetime(2030, 1, 1, tzinfo=UTC)
        key, record = keys.generate(scopes=["orders:read", "orders:read"], expires_at=expiry)
        assert KEY_PATTERN.fullmatch(key)
        assert key.split("_")[1] == record.id
        assert re.fullmatch(r"[0-9a-f]{64}", record.digest)
        assert key.split("_")[2] not in repr(record)
        assert (record.scopes, record.expires_at) == (frozenset({"orders:read"}), expiry)

    def test_keys_are_random(self, keys: FastAPIAPIKeys[APIKeyRecord]) -> None:
        generated = [keys.generate() for _ in range(50)]
        assert (
            len({new.key for new in generated}) == len({new.record.id for new in generated}) == 50
        )

    @pytest.mark.parametrize(
        ("options", "message"),
        [
            ({"scopes": ["has space"]}, "scopes"),
            ({"scopes": [""]}, "scopes"),
            ({"scopes": ['quo"te']}, "scopes"),
            ({"expires_at": datetime(2030, 1, 1)}, "naive"),
        ],
    )
    def test_invalid_options(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        options: dict[str, Any],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            keys.generate(**options)

    def test_another_prefix(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, prefix="sk_live")
        new = keys.generate()
        assert new.key.startswith("sk_live_")
        assert len(new.key.split("_")) == 4

    @pytest.mark.parametrize("prefix", ["", "1ak", "ak_", "a-k", "a" * 33, "ak!"])
    def test_invalid_prefixes(self, store: Store, prefix: str) -> None:
        with pytest.raises(ValueError, match="prefix"):
            FastAPIAPIKeys(store.lookup, prefix=prefix)

    @pytest.mark.parametrize(
        ("options", "message"),
        [({"cache_ttl": -1}, "cache_ttl"), ({"cache_size": 0}, "cache_size")],
    )
    def test_validation(self, store: Store, options: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            FastAPIAPIKeys(store.lookup, **options)

    def test_short_peppers(self, store: Store) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            FastAPIAPIKeys(store.lookup, pepper="short")


class TestAuthenticate:
    async def test_a_valid_key(self, keys: FastAPIAPIKeys[APIKeyRecord], store: Store) -> None:
        new = keys.generate(scopes=["a"])
        record = await keys.authenticate(store.add(new))
        assert_type(record, APIKeyRecord)
        assert record == new.record

    async def test_a_sync_lookup_runs_in_a_thread(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup_sync)
        key = store.add(keys.generate())
        assert_type(await keys.authenticate(key), APIKeyRecord)
        assert store.lookups == [key.split("_")[1]]

    async def test_a_callable_object_with_an_async_call(self, store: Store) -> None:
        class Lookup:
            async def __call__(self, key_id: str) -> APIKeyRecord | None:
                return await store.lookup(key_id)

        keys = FastAPIAPIKeys(Lookup())
        assert await keys.authenticate(store.add(keys.generate()))

    async def test_a_sync_lookup_that_returns_an_awaitable(self, store: Store) -> None:
        # A sync function returning a coroutine, not the coroutine function itself.
        keys = FastAPIAPIKeys(lambda key_id: store.lookup(key_id))  # pylint: disable=unnecessary-lambda
        assert await keys.authenticate(store.add(keys.generate()))

    async def test_its_own_record_type(self) -> None:
        @dataclass
        class Row:
            digest: str
            scopes: str
            expires_at: datetime | None
            owner: str

        rows: dict[str, Row] = {}

        async def lookup(key_id: str) -> Row | None:
            return rows.get(key_id)

        keys = FastAPIAPIKeys(lookup)
        new = keys.generate(scopes=["a", "b"])
        rows[new.record.id] = Row(new.record.digest, "a b", None, "acme")
        row = await keys.authenticate(new.key)
        assert_type(row, Row)
        assert row.owner == "acme"

    @pytest.mark.parametrize(
        "mangle",
        [
            lambda key: None,
            lambda key: "",
            lambda key: key[:-1],
            lambda key: key + "a",
            lambda key: key.upper(),
            lambda key: key.replace("ak_", "xk_"),
            lambda key: key[:-1] + ("a" if key[-1] != "a" else "b"),  # the wrong secret
            lambda key: "ak_" + "a" * 16 + key[19:],  # an unknown id
            lambda key: key + " " * 300,
            lambda key: key.replace("_", "-"),
        ],
    )
    async def test_every_bad_key_gets_the_same_401(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
        mangle: Any,
    ) -> None:
        key = store.add(keys.generate())
        error = await refused(keys, mangle(key))
        assert (error.status_code, error.detail) == (401, "Invalid API key")
        assert error.headers == {"WWW-Authenticate": "APIKey"}

    async def test_malformed_keys_arent_looked_up(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
    ) -> None:
        await refused(keys, "ak_short_key")
        assert store.lookups == []

    @pytest.mark.parametrize(
        "expires_at",
        [
            datetime.now(UTC) - timedelta(seconds=1),
            datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),  # naive: taken as UTC
        ],
    )
    async def test_expired_keys(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
        expires_at: datetime,
    ) -> None:
        new = keys.generate()
        store.add(new._replace(record=replace(new.record, expires_at=expires_at)))
        assert (await refused(keys, new.key)).status_code == 401

    async def test_keys_not_expired_yet(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
    ) -> None:
        later = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        new = keys.generate()
        store.add(new._replace(record=replace(new.record, expires_at=later)))
        assert await keys.authenticate(new.key)

    async def test_a_digest_works_only_for_its_id(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
    ) -> None:
        mine, theirs = keys.generate(), keys.generate()
        store.add(mine)
        # Someone copies the digest of their key into my record.
        store.records[mine.record.id] = replace(mine.record, digest=theirs.record.digest)
        forged = f"ak_{mine.record.id}_{theirs.key.split('_')[2]}"
        assert (await refused(keys, forged)).status_code == 401

    async def test_keys_and_secrets_are_never_logged(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        good = keys.generate()
        expired = keys.generate()
        store.add(good)
        store.add(expired._replace(record=replace(expired.record, expires_at=datetime.now(UTC))))
        wrong = good.key[:-1] + ("a" if good.key[-1] != "a" else "b")
        with caplog.at_level(logging.DEBUG, "fastapi_alcazaba.apikeys"):
            for key in (wrong, expired.key, "ak_" + "a" * 16 + "_" + "b" * 52):
                await refused(keys, key)
        assert caplog.records
        for secret in (good.key.split("_")[2], wrong.split("_")[2], expired.key.split("_")[2]):
            assert secret not in caplog.text


class TestPepper:
    async def test_digests_with_a_pepper(self, store: Store) -> None:
        plain, peppered = FastAPIAPIKeys(store.lookup), FastAPIAPIKeys(store.lookup, pepper=PEPPER)
        new = peppered.generate()
        store.add(new)
        assert await peppered.authenticate(new.key)
        assert (await refused(plain, new.key)).status_code == 401
        assert (await refused(FastAPIAPIKeys(store.lookup, pepper=OLD_PEPPER), new.key)).status_code

    async def test_rotating_it(self, store: Store) -> None:
        old = FastAPIAPIKeys(store.lookup, pepper=OLD_PEPPER)
        rotated = FastAPIAPIKeys(store.lookup, pepper=[PEPPER, OLD_PEPPER])
        before, after = old.generate(), rotated.generate()
        store.add(before)
        store.add(after)
        assert await rotated.authenticate(before.key)
        assert await rotated.authenticate(after.key)
        assert (await refused(old, after.key)).status_code == 401  # made with the new one


class TestCache:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> Clock:
        clock = Clock()
        monkeypatch.setattr("fastapi_alcazaba.apikeys.time", clock)
        return clock

    async def test_records_are_kept_for_cache_ttl(self, store: Store, clock: Clock) -> None:
        keys = FastAPIAPIKeys(store.lookup, cache_ttl=30)
        key = store.add(keys.generate())
        await keys.authenticate(key)
        await keys.authenticate(key)
        assert len(store.lookups) == 1
        del store.records[key.split("_")[1]]  # revoked: still accepted until the entry expires
        clock.now += 29
        assert await keys.authenticate(key)
        clock.now += 1
        assert (await refused(keys, key)).status_code == 401
        assert len(store.lookups) == 2

    @pytest.mark.usefixtures("clock")
    async def test_unknown_ids_arent_kept(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, cache_ttl=30)
        new = keys.generate()
        await refused(keys, new.key)
        store.add(new)
        assert await keys.authenticate(new.key)

    @pytest.mark.usefixtures("clock")
    async def test_the_cache_is_bounded(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, cache_ttl=30, cache_size=2)
        first, second, third = (store.add(keys.generate()) for _ in range(3))
        for key in (first, second, first, third):  # first was used after second: second goes
            await keys.authenticate(key)
        store.lookups.clear()
        for key in (first, third, second):
            await keys.authenticate(key)
        assert store.lookups == [second.split("_")[1]]

    @pytest.mark.usefixtures("clock")
    async def test_forget(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, cache_ttl=30)
        first, second = store.add(keys.generate()), store.add(keys.generate())
        for key in (first, second):
            await keys.authenticate(key)
        keys.forget(first.split("_")[1])
        keys.forget("unknown")
        await keys.authenticate(first)
        await keys.authenticate(second)
        assert len(store.lookups) == 3
        keys.forget()
        await keys.authenticate(second)
        assert len(store.lookups) == 4

    async def test_no_cache_by_default(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        store: Store,
    ) -> None:
        key = store.add(keys.generate())
        await keys.authenticate(key)
        await keys.authenticate(key)
        assert len(store.lookups) == 2


def app_with(keys: FastAPIAPIKeys[APIKeyRecord]) -> TestClient:
    app = FastAPI()

    @app.get("/me")
    async def me(key: Annotated[APIKeyRecord, Depends(keys)]) -> dict[str, str]:
        return {"id": key.id}

    @app.get("/orders")
    async def orders(
        key: Annotated[APIKeyRecord, Depends(keys.require("orders:read"))],
    ) -> dict[str, str]:
        return {"id": key.id}

    @app.delete("/orders", dependencies=[Depends(keys.require("orders:read", "orders:write"))])
    async def delete_orders() -> None:
        pass

    return TestClient(app)


class TestDependency:
    def test_the_key_in_its_header(self, keys: FastAPIAPIKeys[APIKeyRecord], store: Store) -> None:
        new = keys.generate()
        client = app_with(keys)
        store.add(new)
        assert client.get("/me", headers={"X-API-Key": new.key}).json() == {"id": new.record.id}

    def test_no_key(self, keys: FastAPIAPIKeys[APIKeyRecord]) -> None:
        response = app_with(keys).get("/me")
        assert (response.status_code, response.json()) == (401, {"detail": "Invalid API key"})
        assert response.headers["www-authenticate"] == "APIKey"

    def test_bearer_only_when_enabled(self, store: Store) -> None:
        plain, bearer = FastAPIAPIKeys(store.lookup), FastAPIAPIKeys(store.lookup, bearer=True)
        key = store.add(plain.generate())
        authorization = {"Authorization": f"Bearer {key}"}
        assert app_with(plain).get("/me", headers=authorization).status_code == 401
        client = app_with(bearer)
        assert client.get("/me", headers=authorization).status_code == 200
        basic = client.get("/me", headers={"Authorization": "Basic abc"})
        assert (basic.status_code, basic.headers["www-authenticate"]) == (401, "Bearer")

    def test_the_header_comes_first(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, bearer=True)
        key = store.add(keys.generate())
        headers = {"X-API-Key": "ak_wrong", "Authorization": f"Bearer {key}"}
        assert app_with(keys).get("/me", headers=headers).status_code == 401

    async def test_called_directly(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, header="Api-Key")
        key = store.add(keys.generate())
        assert await keys(api_key=key)
        with pytest.raises(HTTPException):
            await keys(authorization=object())  # type: ignore[arg-type]  # bearer is off

    def test_scopes(self, keys: FastAPIAPIKeys[APIKeyRecord], store: Store) -> None:
        client = app_with(keys)
        reader = store.add(keys.generate(scopes=["orders:read"]))
        writer = store.add(keys.generate(scopes=["orders:read", "orders:write"]))
        assert client.get("/orders", headers={"X-API-Key": reader}).status_code == 200
        forbidden = client.delete("/orders", headers={"X-API-Key": reader})
        assert (forbidden.status_code, forbidden.json()) == (403, {"detail": "Insufficient scope"})
        assert "www-authenticate" not in forbidden.headers
        assert client.delete("/orders", headers={"X-API-Key": writer}).status_code == 200
        assert client.delete("/orders").status_code == 401

    def test_scopes_with_bearer(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, bearer=True)
        reader = store.add(keys.generate(scopes=["orders:read"]))
        response = app_with(keys).delete("/orders", headers={"Authorization": f"Bearer {reader}"})
        assert response.status_code == 403
        assert response.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:read orders:write"'
        )

    @pytest.mark.parametrize("scopes", [(), ("has space",), ("",)])
    def test_invalid_required_scopes(
        self,
        keys: FastAPIAPIKeys[APIKeyRecord],
        scopes: tuple[str, ...],
    ) -> None:
        with pytest.raises(ValueError, match="scopes"):
            keys.require(*scopes)

    def test_the_openapi_schema(self, store: Store) -> None:
        keys = FastAPIAPIKeys(store.lookup, header="X-Key", bearer=True, scheme_name="Partner")
        schema = app_with(keys).get("/openapi.json").json()
        assert schema["components"]["securitySchemes"] == {
            "Partner": {"type": "apiKey", "in": "header", "name": "X-Key"},
            "PartnerBearer": {"type": "http", "scheme": "bearer"},
        }
        assert schema["paths"]["/me"]["get"]["security"] == [{"Partner": []}, {"PartnerBearer": []}]
