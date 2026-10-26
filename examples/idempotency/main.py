"""A payments API where a retried request never charges a card twice, thanks to idempotency keys.

    uvicorn examples.idempotency.main:app

Clients send a new `Idempotency-Key` (a random UUID) with each new payment, and the same key when
they retry it after a timeout or a dropped connection:

- A retry gets the first response again, with `Idempotent-Replayed: true`: the card is charged
  once.
- A retry that arrives while the first request still runs gets a 409 with `Retry-After: 1`.
- The same key with another body gets a 422: that's a bug in the client, not a retry.
- Keys are per customer: nobody gets another customer's payment by sending their key.
- `POST /payments` without a key gets a 400.

The responses and the locks live in the cache's backend: InMemoryBackend here, so that it runs
without a server. Production uses Redis or Valkey, so that a retry finds the first response
whichever process it reaches.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import HTTPConnection

from fastapi_alcazaba import Cache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.idempotency import IdempotencyMiddleware, require_idempotency_key

# Stands in for your customers and their access tokens (a database, or a JWT check).
TOKENS = {"alice-token": "alice", "bob-token": "bob"}


def throwaway_secret() -> SecretStr:
    """A random secret, for trying the example: it changes at every start and differs between
    processes."""
    return SecretStr(secrets.token_urlsafe(32))


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Signs the stored responses, so that whoever can write to Redis can't plant one for a
    # retry. Every process needs the same key, or a retry that reaches another process runs
    # again: set CACHE_SIGNING_KEY (secrets.token_urlsafe(32)). With personal data in the
    # responses, pass encryption_key= instead: they're encrypted too.
    cache_signing_key: SecretStr = Field(default_factory=throwaway_secret)


settings = Settings()


class NewPayment(BaseModel):
    amount: int = Field(gt=0, le=1_000_000, description="In cents")
    currency: Literal["EUR", "USD"]
    description: str = Field(default="", max_length=200)


class Payment(NewPayment):
    id: str
    customer: str
    created_at: datetime


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Production: RedisBackend(redis.asyncio.from_url(...)), with the URL in the settings, shared
    # by every process. Payments are a list in memory here, a table in your database.
    Cache(InMemoryBackend(), prefix="payments", signing_key=settings.cache_signing_key).install(
        application
    )
    application.state.payments = []
    yield


app = FastAPI(lifespan=lifespan)


def customer_id(connection: HTTPConnection) -> str:
    """Stands in for your authentication: the customer whose bearer token the request carries,
    "" if none."""
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    return TOKENS.get(token, "") if scheme.lower() == "bearer" else ""


# Keys are looked up among the requests of the same customer. The default, the Authorization
# header, suits API keys that never change; with access tokens that expire, a retry sent with a
# refreshed token would charge the card again.
app.add_middleware(IdempotencyMiddleware, identifier=customer_id)


def current_customer(request: Request) -> str:
    if not (customer := customer_id(request)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return customer


def get_payments(request: Request) -> list[Payment]:
    payments: list[Payment] = request.app.state.payments
    return payments


Customer = Annotated[str, Depends(current_customer)]
# A 400 for requests without the header: a payment must never run without one.
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
Payments = Annotated[list[Payment], Depends(get_payments)]


# pylint: disable-next=unused-argument  # a stand-in: the provider's client sends them all
async def charge_card(customer: str, payment: NewPayment, idempotency_key: str) -> str:
    """Stands in for the payment provider: charges the card, and returns the charge's id. The
    key goes to the provider too, which charges a key once however often it's asked."""
    await asyncio.sleep(1)  # the round trip to the card network
    return f"ch_{secrets.token_hex(8)}"


@app.post("/payments", status_code=status.HTTP_201_CREATED)
async def pay(
    payment: NewPayment,
    customer: Customer,
    key: IdempotencyKey,
    payments: Payments,
) -> Payment:
    """Runs once per Idempotency-Key: retries get this response again."""
    # Server errors aren't kept, so a retry runs again. If this request fails after the charge,
    # the provider's own idempotency keys (Stripe's, Adyen's...) keep the card from being charged
    # twice: pass it this one, per customer like the middleware's.
    charge = await charge_card(customer, payment, idempotency_key=f"{customer}:{key}")
    receipt = Payment(
        id=charge, customer=customer, created_at=datetime.now(UTC), **payment.model_dump()
    )
    payments.append(receipt)
    return receipt


@app.get("/payments")
async def list_payments(customer: Customer, payments: Payments) -> list[Payment]:
    return [payment for payment in payments if payment.customer == customer]


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
