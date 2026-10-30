"""Single sign-on with a SAML 2.0 identity provider (Okta, Entra ID, ADFS, Keycloak...):
pip install "fastapi-alcazaba[auth-strategy-saml]".

    saml = auth.use(SAMLStrategy(
        entity_id="https://example.com/saml/metadata",  # this app, as the IdP knows it
        acs_url="https://example.com/saml/acs",  # where the IdP posts its answer
        idp_entity_id="https://idp.example.com/metadata",
        idp_sso_url="https://idp.example.com/sso",
        idp_certificate=settings.idp_certificate,  # PEM: what the IdP signs with
        verify=find_or_create_user,  # (profile) -> your user
    ))

    @app.get("/saml/login")
    async def login() -> RedirectResponse:
        return saml.redirect()

    @app.post("/saml/acs")
    async def acs(request: Request, user: SAMLUser) -> RedirectResponse:
        sessions.login(request, user.id)
        return RedirectResponse("/", status_code=303)

    @app.get("/saml/metadata")
    async def metadata() -> Response:
        return Response(saml.metadata(), media_type="application/samlmetadata+xml")

where `SAMLUser = Annotated[User, Depends(auth.authenticate("saml"))]`. The XML signatures are
verified with signxml; everything else this module reads comes from what they cover.
"""

import base64
import hashlib
import logging
import re
import secrets
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import HTTPConnection, Request
from starlette.responses import RedirectResponse

from fastapi_alcazaba.auth.strategies.base import AuthenticationFailed, Strategy, call
from fastapi_alcazaba.backends.tiered import shared_backend
from fastapi_alcazaba.core.errors import BackendUnavailable
from fastapi_alcazaba.response_cache.caches import Cache, resolve_cache
from fastapi_alcazaba.security._cookies import CookieSettings, cookie_values

try:
    from lxml import etree
    from signxml.algorithms import DigestAlgorithm, SignatureMethod
    from signxml.exceptions import SignXMLException
    from signxml.verifier import SignatureConfiguration, XMLVerifier
except ImportError as exc:  # pragma: no cover  # the extra isn't installed
    raise ImportError(
        'SAMLStrategy needs signxml: pip install "fastapi-alcazaba[auth-strategy-saml]"'
    ) from exc

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

PROTOCOL = "urn:oasis:names:tc:SAML:2.0:protocol"
ASSERTION = "urn:oasis:names:tc:SAML:2.0:assertion"
METADATA = "urn:oasis:names:tc:SAML:2.0:metadata"
HTTP_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
#: Seconds the user has to log in at the IdP and come back.
REQUEST_LIFETIME = 600
#: The largest form read from the IdP.
MAX_RESPONSE = 256 * 1024

_P, _A = f"{{{PROTOCOL}}}", f"{{{ASSERTION}}}"
_REQUEST_ID = re.compile(r"_[0-9a-f]{40}")
_INSTANT = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?")
# SHA-2 only: SHA-1 signatures can be forged.
_SIGNATURE_METHODS = frozenset(
    {
        SignatureMethod.RSA_SHA256,
        SignatureMethod.RSA_SHA384,
        SignatureMethod.RSA_SHA512,
        SignatureMethod.ECDSA_SHA256,
        SignatureMethod.ECDSA_SHA384,
        SignatureMethod.ECDSA_SHA512,
    }
)
_DIGESTS = frozenset({DigestAlgorithm.SHA256, DigestAlgorithm.SHA384, DigestAlgorithm.SHA512})


@dataclass(frozen=True, slots=True)
class SAMLProfile:
    """Who the IdP says logged in, from its signed assertion."""

    #: The user's id at the IdP (an e-mail address, a persistent id...).
    name_id: str
    name_id_format: str | None
    #: The IdP's entity id.
    issuer: str
    #: The IdP's session, for single logout.
    session_index: str | None
    #: The assertion's attributes: name -> values.
    attributes: Mapping[str, list[str]] = field(default_factory=dict)
    #: What `redirect(relay_state=...)` sent, back from the IdP: never redirect to it unchecked.
    relay_state: str | None = None


class SAMLStrategy(Strategy):
    """Authenticates the requests the IdP posts to the Assertion Consumer Service (the ACS):
    checks the signature of their response with the IdP's certificate, and that it answers the
    login this browser started; the user is what `verify` makes of the profile. Other requests
    carry nothing for it (None): it only goes on the ACS route.

    `redirect()` starts the login (SP-initiated: logins the IdP starts by itself aren't accepted).
    The id of its request waits in a short-lived cookie (`__Host-`, HttpOnly, Secure, and
    SameSite=None: the IdP posts to the ACS from its own site), and the answer must carry it.

    Checked on what the signature covers: the issuer, the audience (`entity_id`), the recipient
    (`acs_url`), the request it answers, when it's valid (with `clock_skew`), and that it's a
    bearer assertion. Each assertion works once: its id is kept in the cache's backend until it
    expires, so a copy is refused by every process. Not supported: encrypted assertions, signed
    requests, single logout.

    :param entity_id: this app's entity id at the IdP (its "SP entity id", or "audience").
    :param acs_url: the ACS's absolute URL, where the IdP posts its answers.
    :param idp_entity_id: the IdP's entity id, the issuer of its answers.
    :param idp_sso_url: the IdP's single sign-on URL (HTTP-Redirect binding).
    :param idp_certificate: the IdP's signing certificate (PEM); a list while it rotates them.
        signxml refuses expired ones.
    :param verify: `verify(profile)`: your user, or None to refuse the login. Async, or a plain
        function, which runs in a thread. By default the user is the SAMLProfile.
    :param name_id_format: the NameID format to ask for (`urn:oasis:names:tc:SAML:1.1:nameid-
        format:emailAddress`...); the IdP's default without one.
    :param clock_skew: seconds of tolerance on the validity times, for clocks out of step.
    :param cache: the Cache whose backend remembers the assertions used; FastAPICache's by
        default. When it fails, logins are refused.
    :param name: what `FastAPIAuth.authenticate` calls it by, and the cookie's name.
    """

    def __init__(
        self,
        *,
        entity_id: str,
        acs_url: str,
        idp_entity_id: str,
        idp_sso_url: str,
        idp_certificate: str | Sequence[str],
        verify: Callable[[SAMLProfile], Any] | None = None,
        name_id_format: str | None = None,
        clock_skew: float = 60,
        cache: Cache | None = None,
        name: str = "saml",
    ) -> None:
        super().__init__(name)
        for url in (acs_url, idp_sso_url):
            if not _secure(url):
                raise ValueError(f"the ACS and the IdP are HTTPS (or http://localhost): {url}")
        certificates = [idp_certificate] if isinstance(idp_certificate, str) else idp_certificate
        if not certificates or not all("BEGIN CERTIFICATE" in pem for pem in certificates):
            raise ValueError("idp_certificate is the IdP's certificate in PEM, or a list of them")
        self.entity_id = entity_id
        self.acs_url = acs_url
        self.idp_entity_id = idp_entity_id
        self.idp_sso_url = idp_sso_url
        self.certificates = list(certificates)
        self.verify = verify
        self.name_id_format = name_id_format
        self.clock_skew = timedelta(seconds=clock_skew)
        self.cache = cache
        self.cookie = CookieSettings(f"__Host-alcazaba-saml-{name}", same_site="none")
        self._config = SignatureConfiguration(
            signature_methods=_SIGNATURE_METHODS, digest_algorithms=_DIGESTS
        )

    def redirect(self, relay_state: str | None = None) -> RedirectResponse:
        """The response that sends the browser to the IdP with an AuthnRequest, and sets the
        cookie that the ACS checks. `relay_state` comes back in the profile (a page to return
        to; check it before redirecting there)."""
        request_id = "_" + secrets.token_hex(20)
        request = etree.Element(
            f"{_P}AuthnRequest",
            nsmap={"samlp": PROTOCOL, "saml": ASSERTION},
            ID=request_id,
            Version="2.0",
            IssueInstant=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            Destination=self.idp_sso_url,
            AssertionConsumerServiceURL=self.acs_url,
            ProtocolBinding=HTTP_POST,
        )
        etree.SubElement(request, f"{_A}Issuer").text = self.entity_id
        if self.name_id_format is not None:
            etree.SubElement(
                request, f"{_P}NameIDPolicy", Format=self.name_id_format, AllowCreate="true"
            )
        compressor = zlib.compressobj(wbits=-15)  # raw DEFLATE: the HTTP-Redirect binding
        deflated = compressor.compress(etree.tostring(request)) + compressor.flush()
        query = {"SAMLRequest": base64.b64encode(deflated).decode()}
        if relay_state is not None:
            query["RelayState"] = relay_state
        separator = "&" if "?" in self.idp_sso_url else "?"
        response = RedirectResponse(
            f"{self.idp_sso_url}{separator}{urlencode(query)}", status_code=302
        )
        response.raw_headers.append(self.cookie.header(request_id, max_age=REQUEST_LIFETIME))
        response.headers["Cache-Control"] = "no-store"
        return response

    def metadata(self) -> str:
        """This app's metadata, for the IdP: its entity id, ACS and NameID format."""
        md = f"{{{METADATA}}}"
        entity = etree.Element(
            f"{md}EntityDescriptor", nsmap={"md": METADATA}, entityID=self.entity_id
        )
        sp = etree.SubElement(
            entity,
            f"{md}SPSSODescriptor",
            AuthnRequestsSigned="false",
            WantAssertionsSigned="true",
            protocolSupportEnumeration=PROTOCOL,
        )
        if self.name_id_format is not None:
            etree.SubElement(sp, f"{md}NameIDFormat").text = self.name_id_format
        etree.SubElement(
            sp,
            f"{md}AssertionConsumerService",
            Binding=HTTP_POST,
            Location=self.acs_url,
            index="0",
            isDefault="true",
        )
        xml: bytes = etree.tostring(entity, xml_declaration=True, encoding="UTF-8")
        return xml.decode()

    async def authenticate(self, connection: HTTPConnection) -> Any:
        form = await _form(connection)
        if form is None or "SAMLResponse" not in form:
            return None
        stored = cookie_values(connection.scope, self.cookie.name)
        request_id = stored[0] if stored else ""
        if not _REQUEST_ID.fullmatch(request_id):
            raise AuthenticationFailed("no login was started in this browser (or it expired)")
        try:
            document = base64.b64decode("".join(form["SAMLResponse"].split()), validate=True)
        except ValueError:
            raise AuthenticationFailed("the SAMLResponse isn't base64") from None
        assertion = self._signed_assertion(document, request_id)
        profile = self._check(assertion, request_id, form.get("RelayState"))
        await self._first_use(assertion)
        if self.verify is None:
            return profile
        user = await call(self.verify, profile)
        if user is None:
            raise AuthenticationFailed("verify() refused the login")
        return user

    def _signed_assertion(self, document: bytes, request_id: str) -> Any:
        """The assertion that the IdP's signature covers, after the response's checks."""
        result, reason = None, ""
        for certificate in self.certificates:
            try:
                result = XMLVerifier().verify(
                    document, x509_cert=certificate, expect_config=self._config
                )
                break
            except (SignXMLException, etree.XMLSyntaxError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
        if result is None or isinstance(result, list):
            raise AuthenticationFailed(f"the signature isn't the IdP's ({reason[:200]})")
        signed = result.signed_xml
        if signed is None:  # pragma: no cover  # signxml gives it whenever the signature holds
            raise AuthenticationFailed("nothing is signed")
        if signed.tag == f"{_P}Response":
            # The whole response is signed: its fields count, and it holds the assertion.
            self._check_response(signed, request_id)
            assertions = signed.findall(f"{_A}Assertion")
            if signed.find(f"{_A}EncryptedAssertion") is not None:
                raise AuthenticationFailed("encrypted assertions aren't supported")
            if len(assertions) != 1:
                raise AuthenticationFailed("the response doesn't hold one assertion")
            return assertions[0]
        if signed.tag != f"{_A}Assertion":
            raise AuthenticationFailed(f"the IdP signed a {etree.QName(signed).localname}")
        # Only the assertion is signed: its own fields decide. The response around it must still
        # say it's a success and, if it says which request it answers, this one.
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        self._check_response(etree.fromstring(document, parser=parser), request_id)
        return signed

    def _check_response(self, response: Any, request_id: str) -> None:
        if response.tag != f"{_P}Response":
            raise AuthenticationFailed("not a SAML response")
        status = response.find(f"{_P}Status/{_P}StatusCode")
        if status is None or status.get("Value") != SUCCESS:
            value = status.get("Value") if status is not None else None
            raise AuthenticationFailed(f"the IdP didn't log the user in: {value}")
        if response.get("InResponseTo") not in (None, request_id):
            raise AuthenticationFailed("the response answers another request")
        if response.get("Destination") not in (None, self.acs_url):
            raise AuthenticationFailed("the response is for another ACS")
        issuer = response.find(f"{_A}Issuer")
        if issuer is not None and _text(issuer) != self.idp_entity_id:
            raise AuthenticationFailed("the response comes from another IdP")

    def _check(self, assertion: Any, request_id: str, relay_state: str | None) -> SAMLProfile:
        now = datetime.now(UTC)
        issuer = assertion.find(f"{_A}Issuer")
        if issuer is None or _text(issuer) != self.idp_entity_id:
            raise AuthenticationFailed("the assertion comes from another IdP")
        name_id = assertion.find(f"{_A}Subject/{_A}NameID")
        if name_id is None or len(name_id) or not _text(name_id):
            raise AuthenticationFailed("the assertion has no NameID")
        confirmed = any(
            self._confirms(data, request_id, now)
            for confirmation in assertion.findall(f"{_A}Subject/{_A}SubjectConfirmation")
            if confirmation.get("Method") == BEARER
            for data in confirmation.findall(f"{_A}SubjectConfirmationData")
        )
        if not confirmed:
            raise AuthenticationFailed("no bearer confirmation for this request, ACS and time")
        conditions = assertion.find(f"{_A}Conditions")
        if conditions is None or not self._in_time(conditions, now):
            raise AuthenticationFailed("the assertion isn't valid now")
        restrictions = conditions.findall(f"{_A}AudienceRestriction")
        if not restrictions or not all(
            self.entity_id in [_text(audience) for audience in restriction.findall(f"{_A}Audience")]
            for restriction in restrictions
        ):
            raise AuthenticationFailed("the assertion is for another audience")
        statement = assertion.find(f"{_A}AuthnStatement")
        session_index = statement.get("SessionIndex") if statement is not None else None
        attributes: dict[str, list[str]] = {}
        for attribute in assertion.findall(f"{_A}AttributeStatement/{_A}Attribute"):
            values = [_text(value) for value in attribute.findall(f"{_A}AttributeValue")]
            attributes.setdefault(attribute.get("Name", ""), []).extend(values)
        return SAMLProfile(
            name_id=_text(name_id),
            name_id_format=name_id.get("Format"),
            issuer=self.idp_entity_id,
            session_index=session_index,
            attributes=attributes,
            relay_state=relay_state,
        )

    def _confirms(self, data: Any, request_id: str, now: datetime) -> bool:
        return (
            data.get("Recipient") == self.acs_url
            and data.get("InResponseTo") == request_id
            and data.get("NotOnOrAfter") is not None
            and self._in_time(data, now)
        )

    def _in_time(self, element: Any, now: datetime) -> bool:
        try:
            not_before = _instant(element.get("NotBefore"))
            not_on_or_after = _instant(element.get("NotOnOrAfter"))
        except ValueError:
            return False
        if not_before is not None and now + self.clock_skew < not_before:
            return False
        return not_on_or_after is None or now - self.clock_skew < not_on_or_after

    async def _first_use(self, assertion: Any) -> None:
        """Refuse an assertion seen before, in any process."""
        assertion_id = assertion.get("ID")
        if not assertion_id:
            raise AuthenticationFailed("the assertion has no ID")
        ends = [
            _instant_or_none(element.get("NotOnOrAfter"))
            for element in assertion.iter(f"{_A}Conditions", f"{_A}SubjectConfirmationData")
        ]
        until = max((end for end in ends if end is not None), default=datetime.now(UTC))
        ttl = int((until + self.clock_skew - datetime.now(UTC)).total_seconds()) + 1
        cache = resolve_cache(self.cache)
        digest = hashlib.sha256(f"{self.idp_entity_id}\0{assertion_id}".encode()).hexdigest()
        key = cache.state_key("saml", f"{self.name}:{digest}")
        backend = shared_backend(cache.backend)
        try:
            uses = await cache.guard.run(
                "incr", lambda: backend.incr(key, 1, max(1, min(ttl, 86_400)))
            )
        except BackendUnavailable as exc:
            raise AuthenticationFailed("the assertion couldn't be checked for replays") from exc
        if uses != 1:
            raise AuthenticationFailed("the assertion was used before (a replay)")


async def _form(connection: HTTPConnection) -> dict[str, str] | None:
    """The fields of a form posted to this request; None when it isn't one."""
    if not isinstance(connection, Request) or connection.method != "POST":
        return None
    content_type = connection.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return None
    length = connection.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_RESPONSE:
        raise AuthenticationFailed("the form is too large")
    body = await connection.body()
    if len(body) > MAX_RESPONSE:
        raise AuthenticationFailed("the form is too large")
    try:
        fields = parse_qs(body.decode("latin-1"), max_num_fields=10)
    except ValueError:
        raise AuthenticationFailed("too many fields in the form") from None
    return {name: values[0] for name, values in fields.items()}


def _text(element: Any) -> str:
    """All the text of an element (comments are already gone from what signxml verified)."""
    return "".join(element.itertext()).strip()


def _instant(value: str | None) -> datetime | None:
    """An xs:dateTime (UTC when it has no zone; fractions of any length)."""
    if value is None:
        return None
    found = _INSTANT.fullmatch(value.strip())
    if found is None:
        raise ValueError(f"not a dateTime: {value!r}")
    seconds, fraction, zone = found.groups()
    text = f"{seconds}.{(fraction or '0')[:6].ljust(6, '0')}"
    zone = "+00:00" if zone in (None, "Z") else zone
    return datetime.fromisoformat(text + zone).astimezone(UTC)


def _instant_or_none(value: str | None) -> datetime | None:
    try:
        return _instant(value)
    except ValueError:
        return None


def _secure(url: str) -> bool:
    """HTTPS, or plain HTTP to this machine (in development)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1")
