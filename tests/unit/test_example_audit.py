"""The audit example: an admin API whose sensitive actions leave a signed trail."""

import dataclasses
import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from examples.audit.main import app, recent, settings, trail_log
from fastapi_alcazaba.audit import AuditEvent, verify_audit_trail

ADA = {"Authorization": "Bearer ada-token"}  # admin
SAM = {"Authorization": "Bearer sam-token"}  # support


@pytest.fixture(name="client")
def fixture_client() -> Iterator[TestClient]:
    recent.events.clear()
    with TestClient(app) as client:
        yield client


def events() -> list[AuditEvent]:
    return list(recent.events)


def test_an_admin_changes_a_role(client: TestClient) -> None:
    response = client.put("/accounts/u3/role", json={"role": "support"}, headers=ADA)

    assert response.json() == {"id": "u3", "email": "vic@example.com", "role": "support"}
    (event,) = events()
    assert (event.action, event.outcome, event.actor, event.resource) == (
        "account.role.change",
        "success",
        "u1",
        "account:u3",
    )
    assert event.details == {"from": "viewer", "to": "support"}
    assert (event.method, event.route) == ("PUT", "/accounts/{account_id}/role")
    assert event.request_id == response.headers["X-Request-ID"]


def test_refusals_are_recorded(client: TestClient) -> None:
    assert client.delete("/accounts/u3", headers=SAM).status_code == 403
    assert client.delete("/accounts/u3").status_code == 401
    assert client.delete("/accounts/u9", headers=ADA).status_code == 404

    forbidden, anonymous, missing = events()
    assert (forbidden.outcome, forbidden.actor, forbidden.reason, forbidden.status) == (
        "denied",
        "u2",
        "support, not admin",
        403,
    )
    assert (anonymous.outcome, anonymous.actor, anonymous.resource) == (
        "denied",
        None,
        "account:u3",
    )
    assert (missing.outcome, missing.status, missing.resource) == ("failure", 404, "account:u9")


def test_deleting_an_account(client: TestClient) -> None:
    assert client.delete("/accounts/u3", headers=ADA).status_code == 204
    assert client.delete("/accounts/u3", headers=ADA).status_code == 404

    deleted, again = events()
    assert (deleted.outcome, deleted.details) == ("success", {"email": "vic@example.com"})
    assert again.outcome == "failure"


def test_signing_in(client: TestClient) -> None:
    good = client.post("/sessions", json={"email": "ada@example.com", "password": "correct horse"})
    wrong = client.post("/sessions", json={"email": "ada@example.com", "password": "hunter2"})
    unknown = client.post("/sessions", json={"email": "eve@example.com", "password": "x"})

    assert good.json() == {"access_token": "ada-token", "token_type": "bearer"}
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Wrong e-mail or password"}
    signed_in, wrong_password, unknown_email = events()
    assert (signed_in.outcome, signed_in.actor, signed_in.route) == ("success", "u1", "/sessions")
    assert (wrong_password.outcome, wrong_password.actor) == ("denied", "u1")
    assert wrong_password.reason == "wrong password"
    assert (unknown_email.actor, unknown_email.reason) == (None, "unknown e-mail")
    assert wrong_password.request_id == wrong.headers["X-Request-ID"]
    assert all("hunter2" not in event.to_json() for event in events())


def test_reading_the_trail_is_recorded_and_for_admins_only(client: TestClient) -> None:
    client.put("/accounts/u3/role", json={"role": "support"}, headers=ADA)
    assert client.get("/audit/events", headers=SAM).status_code == 403

    listed = client.get("/audit/events", headers=ADA).json()

    assert [event["action"] for event in listed] == ["account.role.change", "audit.read"]
    assert [event.action for event in events()][-1] == "audit.read"  # this read, recorded after


def test_verifying_the_trail(client: TestClient) -> None:
    client.put("/accounts/u3/role", json={"role": "support"}, headers=ADA)
    client.delete("/accounts/u2", headers=SAM)

    assert client.get("/audit/verify", headers=ADA).json() == {"events": 2, "problems": []}

    first = recent.events[0]
    recent.events[0] = dataclasses.replace(first, actor="u2")  # someone rewrites who did it
    problems = client.get("/audit/verify", headers=ADA).json()["problems"]
    assert problems == [
        f"event {first.id!r} doesn't match its signature: altered, or not written with this key"
    ]


def test_the_trail_is_a_line_of_json_per_event(client: TestClient) -> None:
    # The example's handler; pytest adds its own to loggers that don't propagate.
    (handler,) = [h for h in trail_log.handlers if type(h) is logging.StreamHandler]
    output = io.StringIO()
    stdout = handler.setStream(output)
    assert stdout is not None
    try:
        client.put("/accounts/u3/role", json={"role": "support"}, headers=ADA)
    finally:
        handler.setStream(stdout)

    (line,) = output.getvalue().splitlines()
    assert json.loads(line) == events()[0].to_dict()
    assert verify_audit_trail([json.loads(line)], settings.audit_signing_key) == []
