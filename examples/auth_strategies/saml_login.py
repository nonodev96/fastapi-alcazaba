"""Single sign-on with a SAML identity provider (Okta, Entra ID, ADFS, Keycloak…), then a session.

    uvicorn examples.auth_strategies.saml_login:app

`/saml/login` sends the browser to the identity provider (the IdP) with a SAML request. The user
logs in there, and the IdP posts its signed answer back to `/saml/acs`, where `SAMLStrategy`
checks the signature with the IdP's certificate, and that the answer is for this app, for this
login and still valid. The app keeps its own user in a session (`SessionStrategy`), and gives
the IdP its metadata at `/saml/metadata`.

Set the IdP's IDP_ENTITY_ID, IDP_SSO_URL and IDP_CERTIFICATE (its signing certificate, in PEM),
and CACHE_SIGNING_KEY (secrets.token_urlsafe(32)), the same in every process, in the environment
or a .env file. At the IdP, register the app with the entity id
http://localhost:8000/saml/metadata and the ACS http://localhost:8000/saml/acs.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAuth, FastAPICache
from fastapi_alcazaba.auth import SessionStrategy
from fastapi_alcazaba.auth.strategies.saml import SAMLProfile, SAMLStrategy
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.security import CSRFMiddleware, ServerSessionMiddleware


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file. The IdP's has no defaults:
    an app must never trust an IdP it wasn't told about."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    base_url: str = "http://localhost:8000"
    idp_entity_id: str
    idp_sso_url: str
    idp_certificate: str
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()  # type: ignore[call-arg]  # the IdP's settings come from the environment


class User(BaseModel):
    id: str
    email: str


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.users: dict[str, User] = {}


db = Database()


def find_or_create_user(profile: SAMLProfile) -> User | None:
    """The IdP says who logged in: our user for that NameID (an e-mail here), created the first
    time."""
    user_id = f"saml:{profile.name_id}"
    if user_id not in db.users:
        db.users[user_id] = User(id=user_id, email=profile.name_id)
    return db.users[user_id]


def load_user(user_id: str) -> User | None:
    return db.users.get(user_id)


auth = FastAPIAuth()
saml = auth.use(
    SAMLStrategy(
        entity_id=f"{settings.base_url}/saml/metadata",  # this app, at the IdP
        acs_url=f"{settings.base_url}/saml/acs",
        idp_entity_id=settings.idp_entity_id,
        idp_sso_url=settings.idp_sso_url,
        idp_certificate=settings.idp_certificate,
        verify=find_or_create_user,
    )
)
sessions = auth.use(SessionStrategy(load_user))
SAMLUser = Annotated[User, Depends(auth.authenticate("saml"))]
CurrentUser = Annotated[User, Depends(auth.authenticate("session"))]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the sessions, and refuses an
    # assertion that another one already accepted.
    FastAPICache.init(InMemoryBackend(), prefix="sso", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(ServerSessionMiddleware)
# The IdP posts to the ACS from its own site: SAMLStrategy checks what CSRF would.
app.add_middleware(CSRFMiddleware, exempt_paths=["/saml/acs"])


@app.get("/saml/login")
async def log_in() -> RedirectResponse:
    """To the IdP's login page, which posts the user back to the ACS."""
    return saml.redirect()


@app.post("/saml/acs")
async def acs(request: Request, user: SAMLUser) -> RedirectResponse:
    """The IdP posts its answer here: the strategy checks it; the session remembers the user from
    now on."""
    sessions.login(request, user.id)
    return RedirectResponse("/me", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/saml/metadata")
async def metadata() -> Response:
    """This app's metadata, for the IdP."""
    return Response(saml.metadata(), media_type="application/samlmetadata+xml")


@app.get("/me")
async def me(user: CurrentUser) -> User:
    return user


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(request: Request, user: CurrentUser) -> None:
    del user  # only to refuse requests without a session
    sessions.logout(request)


if __name__ == "__main__":
    uvicorn.run("saml_login:app", reload=True)
