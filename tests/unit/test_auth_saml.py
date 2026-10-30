"""SAMLStrategy: SP-initiated single sign-on, checked on what the IdP's signature covers."""

import base64
import copy
import zlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from lxml import etree
from signxml.signer import XMLSigner
from starlette.requests import HTTPConnection, Request

from fastapi_alcazaba import Cache, FastAPIAuth
from fastapi_alcazaba.auth import AuthenticationFailed
from fastapi_alcazaba.auth.strategies import SAMLStrategy
from fastapi_alcazaba.auth.strategies.saml import MAX_RESPONSE, SAMLProfile, _instant
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import CSRFMiddleware

P = "urn:oasis:names:tc:SAML:2.0:protocol"
A = "urn:oasis:names:tc:SAML:2.0:assertion"
DS = "http://www.w3.org/2000/09/xmldsig#"
EXCLUSIVE_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
SP = "https://testserver/saml/metadata"
ACS = "https://testserver/saml/acs"
IDP = "https://idp.example.com/metadata"
SSO = "https://idp.example.com/sso"
EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


def instant(delta: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class IdP:
    """Stands in for the identity provider: makes signed responses, as a real one does."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(self.key, hashes.SHA256())
        )
        self.pem = certificate.public_bytes(serialization.Encoding.PEM).decode()

    def sign(self, element: Any) -> Any:
        return XMLSigner(c14n_algorithm=EXCLUSIVE_C14N).sign(element, key=self.key, cert=self.pem)

    def assertion(
        self,
        request_id: str,
        *,
        name_id: str = "ana@example.com",
        assertion_id: str = "_assertion1",
        issuer: str = IDP,
        audience: str = SP,
        recipient: str = ACS,
        method: str = "urn:oasis:names:tc:SAML:2.0:cm:bearer",
        not_on_or_after: float = 300,
        not_before: float = -60,
        signed: bool = True,
    ) -> Any:
        assertion = etree.Element(
            f"{{{A}}}Assertion",
            nsmap={"saml": A},
            ID=assertion_id,
            Version="2.0",
            IssueInstant=instant(),
        )
        etree.SubElement(assertion, f"{{{A}}}Issuer").text = issuer
        if signed:  # where real IdPs put it: after the Issuer
            etree.SubElement(assertion, f"{{{DS}}}Signature", nsmap={"ds": DS}, Id="placeholder")
        subject = etree.SubElement(assertion, f"{{{A}}}Subject")
        etree.SubElement(subject, f"{{{A}}}NameID", Format=EMAIL).text = name_id
        confirmation = etree.SubElement(subject, f"{{{A}}}SubjectConfirmation", Method=method)
        etree.SubElement(
            confirmation,
            f"{{{A}}}SubjectConfirmationData",
            InResponseTo=request_id,
            Recipient=recipient,
            NotOnOrAfter=instant(not_on_or_after),
        )
        conditions = etree.SubElement(
            assertion,
            f"{{{A}}}Conditions",
            NotBefore=instant(not_before),
            NotOnOrAfter=instant(not_on_or_after),
        )
        restriction = etree.SubElement(conditions, f"{{{A}}}AudienceRestriction")
        etree.SubElement(restriction, f"{{{A}}}Audience").text = audience
        etree.SubElement(
            assertion, f"{{{A}}}AuthnStatement", AuthnInstant=instant(), SessionIndex="_session1"
        )
        statement = etree.SubElement(assertion, f"{{{A}}}AttributeStatement")
        for attribute, values in {
            "email": ["ana@example.com"],
            "groups": ["staff", "admins"],
        }.items():
            element = etree.SubElement(statement, f"{{{A}}}Attribute", Name=attribute)
            for value in values:
                etree.SubElement(element, f"{{{A}}}AttributeValue").text = value
        return self.sign(assertion) if signed else assertion

    def response(
        self,
        request_id: str,
        *children: Any,
        status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
        destination: str = ACS,
        issuer: str = IDP,
        in_response_to: str | None = None,
        signed: bool = False,
    ) -> Any:
        response = etree.Element(
            f"{{{P}}}Response",
            nsmap={"samlp": P, "saml": A},
            ID="_response1",
            Version="2.0",
            IssueInstant=instant(),
            Destination=destination,
            InResponseTo=in_response_to or request_id,
        )
        etree.SubElement(response, f"{{{A}}}Issuer").text = issuer
        if signed:
            etree.SubElement(response, f"{{{DS}}}Signature", nsmap={"ds": DS}, Id="placeholder")
        status_element = etree.SubElement(response, f"{{{P}}}Status")
        etree.SubElement(status_element, f"{{{P}}}StatusCode", Value=status)
        for child in children or (self.assertion(request_id),):
            response.append(child)
        return self.sign(response) if signed else response


def encoded(element: Any) -> str:
    return base64.b64encode(etree.tostring(element)).decode()


@pytest.fixture(name="idp")
def fixture_idp() -> IdP:
    return IdP()


def strategy(idp: IdP, **options: Any) -> SAMLStrategy:
    defaults: dict[str, Any] = {
        "entity_id": SP,
        "acs_url": ACS,
        "idp_entity_id": IDP,
        "idp_sso_url": SSO,
        "idp_certificate": idp.pem,
        "name_id_format": EMAIL,
        "cache": Cache(InMemoryBackend()),
    }
    return SAMLStrategy(**(defaults | options))


def client_for(saml: SAMLStrategy) -> TestClient:
    auth = FastAPIAuth(saml)
    app = FastAPI()

    @app.get("/saml/login")
    async def login(return_to: str | None = None) -> Response:
        return saml.redirect(relay_state=return_to)

    @app.post("/saml/acs")
    async def acs(user: Annotated[Any, Depends(auth.authenticate("saml"))]) -> Any:
        if isinstance(user, SAMLProfile):
            return {
                "name_id": user.name_id,
                "format": user.name_id_format,
                "issuer": user.issuer,
                "session_index": user.session_index,
                "attributes": dict(user.attributes),
                "relay_state": user.relay_state,
            }
        return {"user": user}

    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def start(client: TestClient, relay_state: str | None = None) -> str:
    """Start a login; the id of the AuthnRequest."""
    redirect = client.get("/saml/login", params={"return_to": relay_state} if relay_state else None)
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), wbits=-15)
    request_id: str = etree.fromstring(xml).get("ID")
    return request_id


def post(client: TestClient, response: str, relay_state: str | None = None) -> Any:
    form = {"SAMLResponse": response} | ({"RelayState": relay_state} if relay_state else {})
    return client.post("/saml/acs", data=form)


class TestLogin:
    def test_the_redirect_to_the_idp(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        redirect = client.get("/saml/login", params={"return_to": "/reports"})
        location = urlsplit(redirect.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == SSO
        query = parse_qs(location.query)
        assert query["RelayState"] == ["/reports"]
        request = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15))
        assert request.tag == f"{{{P}}}AuthnRequest"
        assert request.get("Destination") == SSO
        assert request.get("AssertionConsumerServiceURL") == ACS
        assert request.get("ProtocolBinding") == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        assert request.findtext(f"{{{A}}}Issuer") == SP
        assert request.find(f"{{{P}}}NameIDPolicy").get("Format") == EMAIL
        cookie = redirect.headers["set-cookie"]
        assert cookie.startswith(f"__Host-alcazaba-saml-saml={request.get('ID')};")
        for attribute in ("Max-Age=600", "Path=/", "HttpOnly", "Secure", "SameSite=None"):
            assert attribute in cookie
        assert redirect.headers["cache-control"] == "no-store"

    @pytest.mark.parametrize("signed", ["assertion", "response", "both"])
    def test_the_profile(self, idp: IdP, signed: str) -> None:
        client = client_for(strategy(idp))
        request_id = start(client, relay_state="/reports")
        assertion = idp.assertion(request_id, signed=signed != "response")
        response = idp.response(request_id, assertion, signed=signed != "assertion")
        assert post(client, encoded(response), relay_state="/reports").json() == {
            "name_id": "ana@example.com",
            "format": EMAIL,
            "issuer": IDP,
            "session_index": "_session1",
            "attributes": {"email": ["ana@example.com"], "groups": ["staff", "admins"]},
            "relay_state": "/reports",
        }

    def test_verify_makes_the_user(self, idp: IdP) -> None:
        users = {"ana@example.com": {"id": 1}}
        client = client_for(strategy(idp, verify=lambda profile: users.get(profile.name_id)))
        request_id = start(client)
        assert post(client, encoded(idp.response(request_id))).json() == {"user": {"id": 1}}
        request_id = start(client)
        eve = idp.assertion(request_id, name_id="eve@example.com", assertion_id="_assertion2")
        other = idp.response(request_id, eve)
        assert post(client, encoded(other)).status_code == 401

    def test_rotated_certificates(self, idp: IdP) -> None:
        new = IdP()
        client = client_for(strategy(idp, idp_certificate=[new.pem, idp.pem]))
        for index, signer in enumerate((idp, new)):
            request_id = start(client)
            assertion = signer.assertion(request_id, assertion_id=f"_assertion{index}")
            response = signer.response(request_id, assertion)
            assert post(client, encoded(response)).status_code == 200

    def test_clock_skew(self, idp: IdP) -> None:
        for skew, status in ((60, 200), (0, 401)):
            client = client_for(strategy(idp, clock_skew=skew))
            request_id = start(client)
            late = idp.assertion(request_id, not_on_or_after=-20)
            assert post(client, encoded(idp.response(request_id, late))).status_code == status


Change = Callable[[IdP, str], Any]
RESPONDER = "urn:oasis:names:tc:SAML:2.0:status:Responder"
SENDER_VOUCHES = "urn:oasis:names:tc:SAML:2.0:cm:sender-vouches"


def with_assertion(**options: Any) -> Change:
    """A response whose signed assertion is made with `options`."""
    return lambda idp, request_id: idp.response(request_id, idp.assertion(request_id, **options))


def with_signed_response(**options: Any) -> Change:
    """A signed response, made with `options`, around an unsigned assertion."""

    def make(idp: IdP, request_id: str) -> Any:
        assertion = idp.assertion(request_id, signed=False)
        return idp.response(request_id, assertion, signed=True, **options)

    return make


def by_another_key(_idp: IdP, request_id: str) -> Any:
    other = IdP()
    return other.response(request_id, other.assertion(request_id))


def for_another_request(idp: IdP, request_id: str) -> Any:
    return idp.response(request_id, idp.assertion("_" + "0" * 40))


def failed_unsigned(idp: IdP, request_id: str) -> Any:
    return idp.response(request_id, idp.assertion(request_id), status=RESPONDER)


def answering_another_request(idp: IdP, request_id: str) -> Any:
    return idp.response(request_id, idp.assertion(request_id), in_response_to="_" + "1" * 40)


def encrypted(idp: IdP, request_id: str) -> Any:
    return idp.response(request_id, etree.Element(f"{{{A}}}EncryptedAssertion"), signed=True)


def two_assertions(idp: IdP, request_id: str) -> Any:
    first = idp.assertion(request_id, signed=False)
    second = idp.assertion(request_id, signed=False, assertion_id="_assertion2")
    return idp.response(request_id, first, second, signed=True)


def bare_assertion(idp: IdP, request_id: str) -> Any:
    return idp.assertion(request_id)  # signed, but not in a response


def with_a_bad_date(idp: IdP, request_id: str) -> Any:
    assertion = idp.assertion(request_id, signed=False)
    assertion.find(f"{{{A}}}Conditions").set("NotOnOrAfter", "soon")
    return idp.response(request_id, assertion, signed=True)


def without_an_id(idp: IdP, request_id: str) -> Any:
    assertion = idp.assertion(request_id, signed=False)
    del assertion.attrib["ID"]
    return idp.response(request_id, assertion, signed=True)


def another_element(idp: IdP, _request_id: str) -> Any:
    return idp.sign(etree.fromstring(b'<Other ID="_x"><a>b</a></Other>'))


REFUSED: dict[str, Change] = {
    "unsigned": with_assertion(signed=False),
    "another-key": by_another_key,
    "audience": with_assertion(audience="https://other"),
    "recipient": with_assertion(recipient="https://other/acs"),
    "issuer": with_assertion(issuer="https://evil"),
    "another-request": for_another_request,
    "expired": with_assertion(not_on_or_after=-300),
    "not-yet": with_assertion(not_before=600),
    "not-bearer": with_assertion(method=SENDER_VOUCHES),
    "no-name-id": with_assertion(name_id=""),
    "status": with_signed_response(status=RESPONDER),
    "status-unsigned": failed_unsigned,
    "destination": with_signed_response(destination="https://other/acs"),
    "response-request": answering_another_request,
    "encrypted": encrypted,
    "two-assertions": two_assertions,
    "other-element": another_element,
    "bare-assertion": bare_assertion,
    "response-issuer": with_signed_response(issuer="https://evil"),
    "bad-date": with_a_bad_date,
    "no-assertion-id": without_an_id,
}


class TestRefused:
    @pytest.mark.parametrize("make", REFUSED.values(), ids=REFUSED.keys())
    def test_responses(self, idp: IdP, make: Change) -> None:
        client = client_for(strategy(idp))
        request_id = start(client)
        refused = post(client, encoded(make(idp, request_id)))
        assert (refused.status_code, refused.json()) == (401, {"detail": "Not authenticated"})

    def test_without_a_login_started_here(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        request_id = start(client)
        client.cookies.clear()
        assert post(client, encoded(idp.response(request_id))).status_code == 401

    def test_replays(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        request_id = start(client)
        response = encoded(idp.response(request_id))
        assert post(client, response).status_code == 200
        assert post(client, response).status_code == 401

    def test_the_replay_check_fails_closed(self, idp: IdP) -> None:
        class Down(InMemoryBackend):
            async def incr(self, key: str, amount: int = 1, expire: int | None = None) -> int:
                raise ConnectionError("down")

        client = client_for(strategy(idp, cache=Cache(Down())))
        request_id = start(client)
        assert post(client, encoded(idp.response(request_id))).status_code == 401

    @pytest.mark.parametrize(
        "body",
        [
            "not base64!",
            base64.b64encode(b"<not xml").decode(),
            base64.b64encode(b'<!DOCTYPE r [<!ENTITY x "y">]><r>&x;</r>').decode(),
        ],
    )
    def test_malformed(self, idp: IdP, body: str) -> None:
        client = client_for(strategy(idp))
        start(client)
        assert post(client, body).status_code == 401

    def test_forms_too_large_or_with_too_many_fields(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        start(client)
        assert post(client, "a" * 300_000).status_code == 401
        fields = {f"field{index}": "x" for index in range(20)} | {"SAMLResponse": "x"}
        assert client.post("/saml/acs", data=fields).status_code == 401

    def test_other_requests_carry_nothing_for_it(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        assert client.post("/saml/acs", json={"SAMLResponse": "x"}).status_code == 401
        assert client.post("/saml/acs", data={"other": "x"}).status_code == 401

    async def test_not_a_post(self, idp: IdP) -> None:
        saml = strategy(idp)
        scope = {"type": "http", "method": "GET", "headers": [], "query_string": b""}
        assert await saml.authenticate(Request(scope)) is None
        assert await saml.authenticate(HTTPConnection(scope)) is None

    async def test_a_large_body_without_its_length(self, idp: IdP) -> None:
        """Streamed without Content-Length: its size is checked once read."""
        chunks = [b"SAMLResponse=" + b"a" * (MAX_RESPONSE + 1)]

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": chunks.pop(), "more_body": False}

        form = [(b"content-type", b"application/x-www-form-urlencoded")]
        request = Request({"type": "http", "method": "POST", "headers": form}, receive)
        with pytest.raises(AuthenticationFailed, match="too large"):
            await strategy(idp).authenticate(request)

    def test_a_malformed_date_elsewhere_is_ignored(self, idp: IdP) -> None:
        """Only the confirmation that matches counts; another one's dates don't break it."""
        client = client_for(strategy(idp))
        request_id = start(client)
        assertion = idp.assertion(request_id, signed=False)
        subject = assertion.find(f"{{{A}}}Subject")
        other = etree.SubElement(subject, f"{{{A}}}SubjectConfirmation", Method="other")
        etree.SubElement(other, f"{{{A}}}SubjectConfirmationData", NotOnOrAfter="whenever")
        response = idp.response(request_id, assertion, signed=True)
        assert post(client, encoded(response)).status_code == 200


class TestAttacks:
    def test_signature_wrapping(self, idp: IdP) -> None:
        """An unsigned assertion for the admin where the signed one was, which is moved away:
        what counts is what the signature covers."""
        client = client_for(strategy(idp))
        request_id = start(client)
        signed = idp.assertion(request_id)
        forged = idp.assertion(
            request_id, name_id="admin@example.com", assertion_id="_forged", signed=False
        )
        response = idp.response(request_id, forged)
        extensions = etree.SubElement(response, f"{{{P}}}Extensions")
        extensions.append(signed)
        logged_in = post(client, encoded(response))
        assert logged_in.json()["name_id"] == "ana@example.com"

    def test_signature_wrapping_with_the_same_id(self, idp: IdP) -> None:
        client = client_for(strategy(idp))
        request_id = start(client)
        forged = idp.assertion(
            request_id, name_id="admin@example.com", assertion_id="_assertion1", signed=False
        )
        response = idp.response(request_id, forged)
        etree.SubElement(response, f"{{{P}}}Extensions").append(idp.assertion(request_id))
        assert post(client, encoded(response)).status_code == 401

    def test_a_comment_in_the_name_id(self, idp: IdP) -> None:
        """Signed for ana@example.com.evil.com, with a comment that would cut it short."""
        client = client_for(strategy(idp))
        request_id = start(client)
        assertion = idp.assertion(request_id, name_id="ana@example.com.evil.com")
        name_id = next(assertion.iter(f"{{{A}}}NameID"))
        name_id.text = "ana@example.com"
        comment = etree.Comment("")
        comment.tail = ".evil.com"
        name_id.append(comment)
        logged_in = post(client, encoded(idp.response(request_id, copy.deepcopy(assertion))))
        assert logged_in.json()["name_id"] == "ana@example.com.evil.com"


class TestConfiguration:
    def test_metadata(self, idp: IdP) -> None:
        metadata = etree.fromstring(strategy(idp).metadata().encode())
        md = "urn:oasis:names:tc:SAML:2.0:metadata"
        assert metadata.get("entityID") == SP
        descriptor = metadata.find(f"{{{md}}}SPSSODescriptor")
        assert descriptor.get("WantAssertionsSigned") == "true"
        assert descriptor.findtext(f"{{{md}}}NameIDFormat") == EMAIL
        service = descriptor.find(f"{{{md}}}AssertionConsumerService")
        assert (service.get("Location"), service.get("Binding")) == (
            ACS,
            "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
        )

    def test_without_a_name_id_format(self, idp: IdP) -> None:
        saml = strategy(idp, name_id_format=None)
        assert "NameIDFormat" not in saml.metadata()
        redirect = client_for(saml).get("/saml/login")
        query = parse_qs(urlsplit(redirect.headers["location"]).query)
        request = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
        assert b"NameIDPolicy" not in request

    def test_errors(self, idp: IdP) -> None:
        with pytest.raises(ValueError, match="HTTPS"):
            strategy(idp, acs_url="http://example.com/acs")
        with pytest.raises(ValueError, match="PEM"):
            strategy(idp, idp_certificate="not a certificate")
        with pytest.raises(ValueError, match="PEM"):
            strategy(idp, idp_certificate=[])
        local = strategy(idp, acs_url="http://localhost:8000/saml/acs", name="dev")
        assert local.cookie.name == "__Host-alcazaba-saml-dev"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2026-10-28T10:00:00Z", datetime(2026, 10, 28, 10, tzinfo=UTC)),
            ("2026-10-28T10:00:00.1234567Z", datetime(2026, 10, 28, 10, 0, 0, 123456, tzinfo=UTC)),
            ("2026-10-28T12:00:00+02:00", datetime(2026, 10, 28, 10, tzinfo=UTC)),
            ("2026-10-28T10:00:00", datetime(2026, 10, 28, 10, tzinfo=UTC)),
        ],
    )
    def test_instants(self, text: str, expected: datetime) -> None:
        assert _instant(text) == expected

    def test_bad_instants(self) -> None:
        with pytest.raises(ValueError, match="not a dateTime"):
            _instant("yesterday")


def test_csrf_lets_the_idp_post_to_the_acs(idp: IdP) -> None:
    """As the documentation says: the IdP posts from its site, so the ACS is exempt."""
    for exempt, status in (((), 403), (["/saml/acs"], 200)):
        client = client_for(strategy(idp))
        client.app.add_middleware(CSRFMiddleware, exempt_paths=exempt)  # type: ignore[attr-defined]
        request_id = start(client)
        response = {"SAMLResponse": encoded(idp.response(request_id))}
        from_idp = {"Sec-Fetch-Site": "cross-site", "Origin": "https://idp.example.com"}
        assert client.post("/saml/acs", data=response, headers=from_idp).status_code == status
