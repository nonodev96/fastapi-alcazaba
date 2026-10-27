"""Operating an app without a deploy: maintenance mode, feature flags and a circuit breaker.

    uvicorn examples.operations.main:app

- `PUT /admin/maintenance` closes the app: a 503 with `Retry-After` for every request but
  `/health` (the load balancer keeps the processes) and `/admin` (you can reopen it).
- `PUT /admin/flags/{name}`: `new-checkout` goes to a percentage of the users, always the same
  ones; `/reports/beta` is a 404 but for the users on the allow list of `beta-reports`.
- `/orders/{order_id}/shipping` calls a flaky shipping service (simulated). After 3 failures in a
  row, each a 502 that waited for it, the breaker opens: a 503 at once for 30 s, without calling
  it. Then one request tries again; `POST /admin/breakers/shipping/close` closes it at once.

The admin routes take `Authorization: Bearer $ADMIN_TOKEN`.

The maintenance switch and the flags live in the backend, shared by every process. Each process
keeps a copy, read again at most every 1 s (maintenance) or 5 s (flags): requests don't each ask
the backend, and a change reaches every process within that. Breakers are each process's own:
one opens after its process's failures, with no round trip on every call.
"""

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import HTTPConnection

from fastapi_alcazaba import (
    BackendUnavailable,
    FastAPIBreaker,
    FastAPICache,
    FastAPIFeatureFlags,
    FastAPIMaintenance,
)
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.operations import MAX_MESSAGE_LENGTH, FeatureFlag, MaintenanceMiddleware
from fastapi_alcazaba.operations.maintenance import MaintenanceStatus

logger = logging.getLogger(__name__)


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file. Set both in production, the
    same in every process (secrets.token_urlsafe(32))."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    admin_token: SecretStr = Field(default_factory=throwaway_secret)
    # Seals the switch and the flags in the backend: a state planted there by someone else is
    # ignored, and so is what a process with another key writes.
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class Flag(StrEnum):
    """The flags this app reads: the admin routes define only these."""

    NEW_CHECKOUT = "new-checkout"
    BETA_REPORTS = "beta-reports"


class Quote(BaseModel):
    order_id: int
    carrier: str
    days: int


class ShippingService:
    """Stands in for a downstream service and its SDK: fails while `down`, and counts the calls
    that reach it."""

    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    async def quote(self, order_id: int) -> Quote:
        self.calls += 1
        if self.down:
            raise ConnectionError("the shipping service doesn't answer")
        return Quote(order_id=order_id, carrier="Alhambra Express", days=2)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Redis or Valkey in production, so that every process shares the switch and the flags:
    # InMemoryBackend keeps them in this process.
    FastAPICache.init(InMemoryBackend(), prefix="ops", signing_key=settings.cache_signing_key)
    application.state.shipping = ShippingService()
    # Only what says the service is down counts, not a bug of ours; a call that hangs is cancelled
    # after 5 s and counts too.
    application.state.shipping_breaker = FastAPIBreaker(
        "shipping", failure_threshold=3, reset_timeout=30, timeout=5, failures=(ConnectionError,)
    )
    if "admin_token" not in settings.model_fields_set:
        token = settings.admin_token.get_secret_value()
        logger.warning("ADMIN_TOKEN isn't set: the admin token of this run is %s", token)
    yield


# How old each process's copy of the shared state may be, in seconds.
maintenance = FastAPIMaintenance(refresh=1)
flags = FastAPIFeatureFlags(refresh=5)

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    MaintenanceMiddleware, maintenance=maintenance, allow_paths=("/health", "/admin")
)


@app.exception_handler(BackendUnavailable)
async def backend_unavailable(_request: Request, _exc: BackendUnavailable) -> JSONResponse:
    """An admin change the backend didn't take: nothing changed, and the response says so."""
    return JSONResponse({"detail": "Not changed: the backend is unavailable"}, status_code=503)


def current_user(connection: HTTPConnection) -> str | None:
    """The user's id. Stands in for your authentication: here the bearer token is the id itself,
    so that you can try it with curl. Take it from a session or a token you verify, or anyone
    could put themselves on an allow list."""
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" and token else None


@app.get("/health")
async def health() -> dict[str, str]:
    """Served during maintenance too: the processes are fine, nothing should restart them."""
    return {"status": "ok"}


@app.get("/checkout")
async def checkout(user: Annotated[str | None, Depends(current_user)]) -> dict[str, str]:
    """The new checkout for the users in the rollout; without a user, a partial rollout is off."""
    new = await flags.enabled(Flag.NEW_CHECKOUT, subject=user)
    return {"checkout": "new" if new else "classic"}


@app.get(
    "/reports/beta",
    dependencies=[Depends(flags.require(Flag.BETA_REPORTS, subject=current_user))],
)
async def beta_report() -> dict[str, str]:
    """A 404 for the users the flag is off for, as if the route didn't exist."""
    return {"report": "Sales by hour"}


@app.get("/orders/{order_id}/shipping")
async def shipping_quote(order_id: int, request: Request) -> Quote:
    """A 502 when the service fails; while the breaker is open, a 503 with Retry-After."""
    breaker: FastAPIBreaker = request.app.state.shipping_breaker
    shipping: ShippingService = request.app.state.shipping
    try:
        async with breaker.attempt():
            return await shipping.quote(order_id)
    except (ConnectionError, TimeoutError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "The shipping service failed") from exc


def require_admin(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
) -> None:
    """Compared in constant time, and as bytes: compare_digest refuses strings with non-ASCII
    characters, which a client can send."""
    token = credentials.credentials if credentials is not None else ""
    if not secrets.compare_digest(token.encode(), settings.admin_token.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})


admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class MaintenanceUpdate(BaseModel):
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_LENGTH)
    retry_after: int | None = Field(default=None, ge=1)


class FlagUpdate(BaseModel):
    enabled: bool
    # None: everybody, or only the allow list when there's one.
    percentage: float | None = Field(default=None, ge=0, le=100)
    allow: list[str] = Field(default=[], max_length=1000)
    deny: list[str] = Field(default=[], max_length=1000)


@admin.put("/maintenance")
async def start_maintenance(update: MaintenanceUpdate) -> MaintenanceStatus:
    """Close the app, in every process within a second."""
    return await maintenance.enable(update.message, update.retry_after)


@admin.delete("/maintenance", status_code=status.HTTP_204_NO_CONTENT)
async def end_maintenance() -> None:
    await maintenance.disable()


@admin.put("/flags/{name}")
async def set_flag(name: Flag, update: FlagUpdate) -> FeatureFlag:
    """Define a flag, for every process within 5 seconds. The lists come back as digests: the
    backend never gets the users' ids."""
    return await flags.set(name, **update.model_dump())


@admin.put("/simulate/shipping", status_code=status.HTTP_204_NO_CONTENT)
async def simulate_shipping(down: bool, request: Request) -> None:
    """Only in this example: makes the shipping service fail, to watch the breaker open."""
    request.app.state.shipping.down = down


@admin.post("/breakers/shipping/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_shipping_breaker(request: Request) -> None:
    """The shipping service is back: call it again now, instead of waiting for the trial. Only
    in the process that gets this request: each process has its own breaker."""
    request.app.state.shipping_breaker.reset()


@admin.get("/status")
async def status_report(request: Request) -> dict[str, Any]:
    """The shared state as the backend has it now (this process's copies are updated with it),
    and the breaker as this process sees it."""
    breaker: FastAPIBreaker = request.app.state.shipping_breaker
    return {
        "maintenance": await maintenance.status(),
        "flags": {flag: await flags.get(flag) for flag in Flag},
        "process": {"pid": os.getpid(), "shipping_breaker": breaker.state},
    }


app.include_router(admin)


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
