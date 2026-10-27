"""An orders API for machine clients, whose owners sign in with a code sent by e-mail.

    uvicorn examples.auth.main:app

People sign in with a one-time code (`OneTimeCodes`) and get an access token, which logging out
revokes (`TokenBlocklist`). With it they create API keys (`FastAPIAPIKeys`) for their programs:
shown once, stored as digests, with scopes and an expiry date. The programs call `/orders` with
the key in `X-API-Key`.

Revoked tokens, codes and rate-limit counters live in the cache's backend: in memory here, Redis
or Valkey in production, so that every process sees them at once. Set CACHE_SIGNING_KEY,
OTP_SECRET, API_KEY_PEPPER and TOKEN_SECRET (secrets.token_urlsafe(32) each), the same in every
process, in the environment or a .env file.
"""

import base64
import dataclasses
import hmac
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

import uvicorn
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba import FastAPIAPIKeys, FastAPICache
from fastapi_alcazaba.auth import OneTimeCodes, TokenBlocklist
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.rate_limit import FastAPIRateLimiter, too_many_requests

TOKEN_LIFETIME = 15 * 60  # seconds


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file: secrets.token_urlsafe(32)
    each, the same in every process."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)
    otp_secret: SecretStr = Field(default_factory=throwaway_secret)
    api_key_pepper: SecretStr = Field(default_factory=throwaway_secret)
    token_secret: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class Scope(StrEnum):
    # An enum: users can only grant scopes the API knows.
    ORDERS_READ = "orders:read"
    ORDERS_WRITE = "orders:write"


@dataclasses.dataclass
class APIKeyRow:
    """A row of the api_keys table: everything about a key but the key itself."""

    id: str
    owner: str
    digest: str
    scopes: frozenset[str]
    expires_at: datetime | None


class Order(BaseModel):
    item: str = Field(max_length=100)
    quantity: int = Field(ge=1, le=100)


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.users = {"ana@example.com", "ben@example.com"}
        self.api_keys: dict[str, APIKeyRow] = {}
        self.orders: dict[str, list[Order]] = {}  # by owner


async def send_email(to: str, text: str) -> None:
    """Stands in for the e-mail service: prints the message, for you to read the code in the
    server's terminal, and keeps it for the tests."""
    outbox.append((to, text))
    print(f"E-mail to {to}: {text}", flush=True)


class Claims(BaseModel):
    sub: str
    jti: str
    iat: int
    exp: int


class AccessTokens:
    """Stands in for a JWT library, which this example doesn't depend on: a JWT's claims, signed
    with HMAC-SHA256. A real app issues and decodes its JWTs with PyJWT or joserfc, and gives the
    blocklist their `jti`, `sub`, `iat` and `exp` just like here."""

    def __init__(self, key: SecretStr) -> None:
        self.key = key.get_secret_value().encode()

    def sign(self, payload: bytes) -> bytes:
        return hmac.digest(self.key, payload, "sha256").hex().encode()

    def issue(self, subject: str) -> str:
        now = int(time.time())
        jti = secrets.token_urlsafe(16)
        claims = Claims(sub=subject, jti=jti, iat=now, exp=now + TOKEN_LIFETIME)
        payload = base64.urlsafe_b64encode(claims.model_dump_json().encode())
        return (payload + b"." + self.sign(payload)).decode()

    def decode(self, token: str) -> Claims | None:
        """The claims of a token signed here that hasn't expired; None otherwise."""
        payload, _, signature = token.encode().partition(b".")
        if not hmac.compare_digest(signature, self.sign(payload)):
            return None
        claims = Claims.model_validate_json(base64.urlsafe_b64decode(payload))
        return claims if claims.exp > time.time() else None


db = Database()
outbox: list[tuple[str, str]] = []
access_tokens = AccessTokens(settings.token_secret)
blocklist = TokenBlocklist()  # in FastAPICache's backend
codes = OneTimeCodes(secret=settings.otp_secret)  # 6 digits, 5 minutes, 5 guesses
# Per address, wherever the requests come from. Each code replaces the previous one and its
# guesses, so this is what bounds the guesses at an account: 3 codes x 5 guesses per 15 minutes.
code_requests = FastAPIRateLimiter(times=3, minutes=15, identifier=lambda _: "")


async def load_key(key_id: str) -> APIKeyRow | None:
    return db.api_keys.get(key_id)


# A distinctive prefix, for secret scanners to spot leaked keys. Each process keeps the records
# it looked up for 30 s: a revocation takes up to that long to reach the other processes.
keys = FastAPIAPIKeys(load_key, pepper=settings.api_key_pepper, prefix="acme", cache_ttl=30)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the state. Signed: revocations
    # planted in the backend are ignored.
    FastAPICache.init(InMemoryBackend(), prefix="auth", signing_key=settings.cache_signing_key)
    yield


app = FastAPI(lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


async def token_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Claims:
    """The claims of the request's access token; a 401 if it's missing, expired or revoked."""
    claims = access_tokens.decode(credentials.credentials) if credentials else None
    if claims is None or await blocklist.is_revoked(
        claims.jti, subject=claims.sub, issued_at=claims.iat
    ):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return claims


TokenClaims = Annotated[Claims, Depends(token_claims)]


class CodeRequest(BaseModel):
    email: str = Field(max_length=254)


class Login(CodeRequest):
    code: str = Field(max_length=10)


class Token(BaseModel):
    access_token: str
    expires_in: int = TOKEN_LIFETIME


@app.post("/login/code", status_code=status.HTTP_202_ACCEPTED)
async def send_code(form: CodeRequest, request: Request, tasks: BackgroundTasks) -> dict[str, str]:
    """E-mail a login code. The answer doesn't tell whether the address has an account."""
    email = form.email.lower()
    if (wait := await code_requests.hit(request, key=email)) is not None:
        too_many_requests(request, wait)
    tasks.add_task(email_code, email)  # after the response: its timing doesn't tell either
    return {"detail": "If the address has an account, a code is on its way."}


async def email_code(email: str) -> None:
    if email in db.users:
        code = await codes.issue(email, purpose="login")
        await send_email(email, f"Your login code is {code}. It expires in 5 minutes.")


@app.post("/login")
async def login(form: Login) -> Token:
    email = form.email.lower()
    if not await codes.verify(email, form.code, purpose="login"):
        raise HTTPException(400, "Invalid or expired code")
    return Token(access_token=access_tokens.issue(email))


@app.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(claims: TokenClaims) -> None:
    """Refuse this token from now on, until it would have expired anyway."""
    await blocklist.revoke(claims.jti, expires_at=claims.exp)


@app.post("/logout/everywhere")
async def logout_everywhere(claims: TokenClaims) -> Token:
    """Refuse every token of the user issued before this second (a lost laptop, a new password),
    and give this session a new one, which stays valid."""
    await blocklist.revoke_subject(claims.sub, ttl=TOKEN_LIFETIME)
    return Token(access_token=access_tokens.issue(claims.sub))


class NewKey(BaseModel):
    id: str
    key: str
    scopes: list[str]
    expires_at: datetime | None


@app.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_key(
    claims: TokenClaims,
    scopes: Annotated[set[Scope], Body(embed=True, min_length=1)],
) -> NewKey:
    """A new key, shown this once: only a digest of it is stored."""
    expires_at = datetime.now(UTC) + timedelta(days=90)
    key, record = keys.generate(scopes=scopes, expires_at=expires_at)
    db.api_keys[record.id] = APIKeyRow(owner=claims.sub, **dataclasses.asdict(record))
    return NewKey(id=record.id, key=key, scopes=sorted(scopes), expires_at=expires_at)


@app.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: str, claims: TokenClaims) -> None:
    row = db.api_keys.get(key_id)
    if row is None or row.owner != claims.sub:
        raise HTTPException(404, "No such key")
    del db.api_keys[key_id]
    keys.forget(key_id)  # refused at once by this process, by the others within cache_ttl


@app.get("/orders")
async def list_orders(
    key: Annotated[APIKeyRow, Depends(keys.require(Scope.ORDERS_READ))],
) -> list[Order]:
    return db.orders.get(key.owner, [])


@app.post("/orders", status_code=status.HTTP_201_CREATED)
async def add_order(
    order: Order,
    key: Annotated[APIKeyRow, Depends(keys.require(Scope.ORDERS_WRITE))],
) -> Order:
    db.orders.setdefault(key.owner, []).append(order)
    return order


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
