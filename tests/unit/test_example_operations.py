"""The operations example: maintenance mode, feature flags and a circuit breaker, switched from
admin routes."""

import re
import time
from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from typing import Any, TypeVar

import pytest
from fastapi.testclient import TestClient

from examples.operations.main import ShippingService, app, flags, maintenance, settings
from fastapi_alcazaba import FastAPIBreaker, FastAPICache, FastAPIFeatureFlags, FastAPIMaintenance

T = TypeVar("T")

ADMIN_TOKEN = settings.admin_token.get_secret_value()
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
NOT_FOUND = {"detail": "Not Found"}


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    with TestClient(app) as client:
        # The switch and the flags outlive the app: read the new backend now, or the copies the
        # last test left in this process would be used for up to `refresh` seconds.
        assert client.get("/admin/status", headers=ADMIN).status_code == 200
        yield client
    FastAPICache.reset()


def user(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def shipping() -> ShippingService:
    service: ShippingService = app.state.shipping
    return service


def run(client: TestClient, call: Callable[[], Awaitable[T]]) -> T:
    """Run `call` in the app's event loop, as another process of the app would."""
    assert client.portal is not None
    return client.portal.call(call)


def set_flag(client: TestClient, name: str, **definition: Any) -> dict[str, Any]:
    response = client.put(f"/admin/flags/{name}", json=definition, headers=ADMIN)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def checkouts(client: TestClient, users: list[str]) -> dict[str, str]:
    return {u: client.get("/checkout", headers=user(u)).json()["checkout"] for u in users}


class TestAdminRoutes:
    def test_they_take_the_admin_token(self, client: TestClient) -> None:
        assert client.get("/admin/status", headers=ADMIN).status_code == 200
        for headers in ({}, user("user-42"), {"Authorization": f"Basic {ADMIN_TOKEN}"}):
            refused = client.put("/admin/maintenance", json={}, headers=headers)
            assert refused.status_code == 401
            assert refused.headers["www-authenticate"] == "Bearer"
        assert client.get("/checkout").status_code == 200  # nothing changed

    def test_a_token_with_other_characters_is_refused_too(self, client: TestClient) -> None:
        # Compared as bytes: compare_digest would raise on these as strings, a 500.
        headers = {b"Authorization": "Bearer señal".encode("latin-1")}
        assert client.get("/admin/status", headers=headers).status_code == 401


class TestMaintenance:
    def test_closes_the_app(self, client: TestClient) -> None:
        update = {"message": "Back at 10:30", "retry_after": 900}
        state = client.put("/admin/maintenance", json=update, headers=ADMIN).json()
        assert {key: state[key] for key in ("enabled", "message", "retry_after")} == {
            "enabled": True,
            **update,
        }
        for path in ("/checkout", "/orders/7/shipping", "/reports/beta", "/docs"):
            closed = client.get(path)
            assert (closed.status_code, closed.json()) == (503, {"detail": "Back at 10:30"})
            assert closed.headers["retry-after"] == "900"
            assert closed.headers["cache-control"] == "no-store"
        assert shipping().calls == 0

    def test_health_and_admin_are_still_served(self, client: TestClient) -> None:
        client.put("/admin/maintenance", json={}, headers=ADMIN)
        assert client.get("/health").json() == {"status": "ok"}
        status = client.get("/admin/status", headers=ADMIN).json()
        assert status["maintenance"]["enabled"] is True

    def test_reopens(self, client: TestClient) -> None:
        client.put("/admin/maintenance", json={}, headers=ADMIN)
        assert client.delete("/admin/maintenance", headers=ADMIN).status_code == 204
        assert client.get("/checkout").json() == {"checkout": "classic"}

    def test_without_message_or_retry_after(self, client: TestClient) -> None:
        client.put("/admin/maintenance", json={}, headers=ADMIN)
        closed = client.get("/checkout")
        assert (closed.status_code, closed.json()) == (503, {"detail": "Service Unavailable"})
        assert "retry-after" not in closed.headers

    @pytest.mark.parametrize("update", [{"message": "x" * 1001}, {"retry_after": 0}])
    def test_invalid_changes_are_refused(self, client: TestClient, update: dict[str, Any]) -> None:
        assert client.put("/admin/maintenance", json=update, headers=ADMIN).status_code == 422
        assert client.get("/checkout").status_code == 200

    def test_a_change_the_backend_didnt_take_is_a_503(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def down(*args: object) -> None:
            raise ConnectionError("the backend is down")

        monkeypatch.setattr(FastAPICache.get_backend(), "set", down)
        refused = client.put("/admin/maintenance", json={}, headers=ADMIN)
        assert (refused.status_code, refused.json()) == (
            503,
            {"detail": "Not changed: the backend is unavailable"},
        )
        assert client.get("/checkout").status_code == 200


class TestFeatureFlags:
    def test_an_undefined_flag_is_off(self, client: TestClient) -> None:
        assert client.get("/checkout", headers=user("user-42")).json() == {"checkout": "classic"}
        assert client.get("/reports/beta", headers=user("user-42")).json() == NOT_FOUND

    def test_a_rollout_to_a_percentage_of_the_users(self, client: TestClient) -> None:
        users = [f"user-{n}" for n in range(40)]
        assert set_flag(client, "new-checkout", enabled=True, percentage=50)["percentage"] == 50
        half = checkouts(client, users)
        assert 10 < list(half.values()).count("new") < 30
        assert checkouts(client, users) == half  # sticky: each user keeps their answer
        set_flag(client, "new-checkout", enabled=True, percentage=80)
        more = checkouts(client, users)
        assert all(more[u] == "new" for u in users if half[u] == "new")  # only adds users
        assert list(more.values()).count("new") > list(half.values()).count("new")
        set_flag(client, "new-checkout", enabled=True, percentage=100)
        assert set(checkouts(client, users).values()) == {"new"}

    def test_anonymous_users_are_out_of_a_partial_rollout(self, client: TestClient) -> None:
        set_flag(client, "new-checkout", enabled=True, percentage=99)
        assert client.get("/checkout").json() == {"checkout": "classic"}

    def test_an_allow_list_hides_the_route_from_everybody_else(self, client: TestClient) -> None:
        # No percentage: only the allow list.
        set_flag(client, "beta-reports", enabled=True, allow=["user-42"])
        assert client.get("/reports/beta", headers=user("user-42")).json() == {
            "report": "Sales by hour"
        }
        for headers in (user("user-7"), {}):
            hidden = client.get("/reports/beta", headers=headers)
            assert (hidden.status_code, hidden.json()) == (404, NOT_FOUND)
        assert client.get("/no-such-page").json() == NOT_FOUND  # what any missing route says

    def test_deny_wins_and_disabled_is_off_for_everybody(self, client: TestClient) -> None:
        set_flag(client, "beta-reports", enabled=True, percentage=100, deny=["user-7"])
        assert client.get("/reports/beta", headers=user("user-7")).status_code == 404
        assert client.get("/reports/beta", headers=user("user-8")).status_code == 200
        set_flag(client, "beta-reports", enabled=False, percentage=100, allow=["user-42"])
        assert client.get("/reports/beta", headers=user("user-42")).status_code == 404

    def test_the_backend_gets_digests_not_user_ids(self, client: TestClient) -> None:
        flag = set_flag(client, "beta-reports", enabled=True, percentage=0, allow=["user-42"])
        [digest] = flag["allow"]
        assert re.fullmatch("[0-9a-f]{64}", digest)  # SHA-256, salted with the flag's name
        assert "user-42" not in str(flag)
        assert client.get("/admin/status", headers=ADMIN).json()["flags"]["beta-reports"] == flag

    @pytest.mark.parametrize(
        ("name", "definition"),
        [
            ("new-checkout", {"enabled": True, "percentage": 101}),
            ("old-checkout", {"enabled": True, "percentage": 100}),  # not a flag of the app
        ],
    )
    def test_invalid_definitions_are_refused(
        self,
        client: TestClient,
        name: str,
        definition: dict[str, Any],
    ) -> None:
        response = client.put(f"/admin/flags/{name}", json=definition, headers=ADMIN)
        assert response.status_code == 422


class TestCircuitBreaker:
    def test_asks_the_service(self, client: TestClient) -> None:
        quote = client.get("/orders/7/shipping").json()
        assert quote == {"order_id": 7, "carrier": "Alhambra Express", "days": 2}
        assert shipping().calls == 1

    def test_failures_in_a_row_open_it(self, client: TestClient) -> None:
        client.put("/admin/simulate/shipping?down=true", headers=ADMIN)
        for _ in range(3):
            failed = client.get("/orders/7/shipping")
            assert (failed.status_code, failed.json()) == (
                502,
                {"detail": "The shipping service failed"},
            )
        refused = client.get("/orders/7/shipping")
        assert (refused.status_code, refused.json()) == (503, {"detail": "Service Unavailable"})
        assert refused.headers["retry-after"] == "30"
        assert shipping().calls == 3  # the refused request didn't reach it
        process = client.get("/admin/status", headers=ADMIN).json()["process"]
        assert process["shipping_breaker"] == "open"

    def test_a_success_starts_the_count_again(self, client: TestClient) -> None:
        statuses = []
        for down in ("true", "false", "true"):
            client.put(f"/admin/simulate/shipping?down={down}", headers=ADMIN)
            statuses += [client.get("/orders/7/shipping").status_code for _ in range(2)]
        assert statuses == [502, 502, 200, 200, 502, 502]
        assert client.get("/orders/7/shipping").status_code == 502  # the 3rd failure in a row
        assert client.get("/orders/7/shipping").status_code == 503

    def test_then_one_request_tries_again(self, client: TestClient) -> None:
        # The same breaker, but open for 0.2 s instead of 30.
        app.state.shipping_breaker = FastAPIBreaker(
            "shipping", failure_threshold=3, reset_timeout=0.2, failures=(ConnectionError,)
        )
        client.put("/admin/simulate/shipping?down=true", headers=ADMIN)
        assert [client.get("/orders/7/shipping").status_code for _ in range(4)] == [
            502,
            502,
            502,
            503,
        ]
        time.sleep(0.21)
        assert client.get("/orders/7/shipping").status_code == 502  # the trial failed: open again
        assert client.get("/orders/7/shipping").status_code == 503
        client.put("/admin/simulate/shipping?down=false", headers=ADMIN)
        time.sleep(0.21)
        assert client.get("/orders/7/shipping").status_code == 200  # the trial closes it
        assert client.get("/orders/7/shipping").status_code == 200
        assert shipping().calls == 6

    def test_closed_by_hand_when_the_service_is_back(self, client: TestClient) -> None:
        client.put("/admin/simulate/shipping?down=true", headers=ADMIN)
        assert [client.get("/orders/7/shipping").status_code for _ in range(4)] == [
            502,
            502,
            502,
            503,
        ]
        client.put("/admin/simulate/shipping?down=false", headers=ADMIN)
        closed = client.post("/admin/breakers/shipping/close", headers=ADMIN)
        assert closed.status_code == 204
        assert client.get("/orders/7/shipping").status_code == 200  # no 30 s wait
        process = client.get("/admin/status", headers=ADMIN).json()["process"]
        assert process["shipping_breaker"] == "closed"


class TestSharedState:
    """Another process shares the backend but keeps its own copies: here, other instances of the
    switch and the flags. `refresh` is raised so that the copies can't expire mid-test."""

    def test_maintenance_from_another_process(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(maintenance, "refresh", 60)
        run(client, partial(FastAPIMaintenance().enable, "Back soon"))
        assert client.get("/checkout").status_code == 200  # this process's copy, still open
        status = client.get("/admin/status", headers=ADMIN).json()  # reads the backend
        assert status["maintenance"]["message"] == "Back soon"
        assert client.get("/checkout").status_code == 503  # ...and updates the copy

    def test_flags_from_another_process(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(flags, "refresh", 60)
        other = FastAPIFeatureFlags()
        run(client, partial(other.set, "new-checkout", enabled=True, percentage=100))
        assert client.get("/checkout", headers=user("user-42")).json()["checkout"] == "classic"
        client.get("/admin/status", headers=ADMIN)
        assert client.get("/checkout", headers=user("user-42")).json()["checkout"] == "new"
