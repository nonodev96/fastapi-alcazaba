"""The SAML example: single sign-on with an identity provider, then a session."""

import base64
import importlib
import sys
import zlib
from collections.abc import Iterator
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from lxml import etree

from fastapi_alcazaba import FastAPICache
from tests.unit.test_auth_saml import IdP, encoded

MODULE = "examples.auth_strategies.saml_login"
IDP_ENTITY_ID = "https://idp.example.com/metadata"
SP = "http://localhost:8000/saml/metadata"
ACS = "http://localhost:8000/saml/acs"


@pytest.fixture(name="idp")
def fixture_idp() -> IdP:
    return IdP()


@pytest.fixture(name="example")
def fixture_example(monkeypatch: pytest.MonkeyPatch, idp: IdP) -> Iterator[ModuleType]:
    """The example, imported with the IdP's settings in the environment, as it's run."""
    monkeypatch.setenv("IDP_ENTITY_ID", IDP_ENTITY_ID)
    monkeypatch.setenv("IDP_SSO_URL", "https://idp.example.com/sso")
    monkeypatch.setenv("IDP_CERTIFICATE", idp.pem)
    sys.modules.pop(MODULE, None)
    yield importlib.import_module(MODULE)
    sys.modules.pop(MODULE, None)


@pytest.fixture(name="client")
def fixture_client(example: ModuleType) -> Iterator[TestClient]:
    # https: the cookies are Secure.
    with TestClient(example.app, base_url="https://testserver", follow_redirects=False) as client:
        yield client
    FastAPICache.reset()


def start(client: TestClient) -> str:
    """Start a login; the id of the SAML request."""
    redirect = client.get("/saml/login")
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    request = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15))
    request_id: str = request.get("ID")
    return request_id


def answer(idp: IdP, request_id: str, name_id: str = "ana@example.com") -> str:
    """The IdP's signed answer to that request."""
    assertion = idp.assertion(
        request_id, name_id=name_id, issuer=IDP_ENTITY_ID, audience=SP, recipient=ACS
    )
    return encoded(idp.response(request_id, assertion, issuer=IDP_ENTITY_ID, destination=ACS))


class TestSAMLExample:
    def test_the_redirect_to_the_idp(self, client: TestClient) -> None:
        location = urlsplit(client.get("/saml/login").headers["location"])
        assert (location.netloc, location.path) == ("idp.example.com", "/sso")

    def test_logging_in_starts_a_session(self, client: TestClient, idp: IdP) -> None:
        assert client.get("/me").status_code == 401
        response = answer(idp, start(client))
        posted = client.post("/saml/acs", data={"SAMLResponse": response})
        assert (posted.status_code, posted.headers["location"]) == (303, "/me")
        ana = {"id": "saml:ana@example.com", "email": "ana@example.com"}
        assert client.get("/me").json() == ana

    def test_an_answer_works_once(self, client: TestClient, idp: IdP) -> None:
        response = answer(idp, start(client))
        assert client.post("/saml/acs", data={"SAMLResponse": response}).status_code == 303
        client.cookies.clear()
        start(client)
        assert client.post("/saml/acs", data={"SAMLResponse": response}).status_code == 401

    def test_another_idps_answer_is_refused(self, client: TestClient) -> None:
        response = answer(IdP(), start(client))  # signed with another key
        assert client.post("/saml/acs", data={"SAMLResponse": response}).status_code == 401
        assert client.get("/me").status_code == 401

    def test_the_apps_metadata(self, client: TestClient) -> None:
        metadata = client.get("/saml/metadata")
        assert metadata.headers["content-type"] == "application/samlmetadata+xml"
        assert etree.fromstring(metadata.content).get("entityID") == SP

    def test_the_idps_settings_are_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IDP_CERTIFICATE", raising=False)
        sys.modules.pop(MODULE, None)
        with pytest.raises(ValueError, match="idp_certificate"):
            importlib.import_module(MODULE)
        sys.modules.pop(MODULE, None)
