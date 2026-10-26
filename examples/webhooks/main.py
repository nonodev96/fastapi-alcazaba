"""A shop that receives signed webhooks from its payment provider, and sends signed webhooks to
the URLs its users register.

    export PAYMENTS_WEBHOOK_SECRET=whsec_...  # the provider's secret for this endpoint
    uvicorn examples.webhooks.main:app

- `POST /webhooks/payments`: the provider's events, signed, recent and each delivery once
  (`FastAPIWebhooks`): a captured request sent again gets a 401. The provider's retries keep the
  `webhook-id`, so the handler is idempotent on it.
- `POST /endpoints`: a user registers a URL and gets the secret to check the shop's events with.
  A signed `ping` goes there first, through `SSRFGuardTransport`: a URL that isn't on the public
  internet (`http://169.254.169.254/`, `http://localhost`, a private network) gets a 422.
- When an order is paid, every endpoint gets a signed `order.paid` event after the response,
  through `ResilientTransport`: a deadline for each call and a circuit breaker for each endpoint.

Replays are remembered in the cache's backend: InMemoryBackend here, Redis or Valkey in
production, so that every process refuses them.
"""

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any

import httpx2 as httpx
import uvicorn
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, HttpUrl, ValidationError

from fastapi_alcazaba import FastAPICache
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.outbound import (
    BlockedDestinationError,
    CircuitOpenError,
    ResilientTransport,
    SSRFGuardTransport,
)
from fastapi_alcazaba.webhooks import FastAPIWebhooks, WebhookMessage, WebhookSigner

# The provider gives it when the endpoint is set up. Without it, a throwaway one for local
# development, which nobody else can sign with.
PAYMENTS_SECRET = os.environ.get("PAYMENTS_WEBHOOK_SECRET") or WebhookSigner.generate_secret()
payment_webhooks = FastAPIWebhooks(PAYMENTS_SECRET)


class Payment(BaseModel):
    order_id: str
    amount: int  # in cents


class PaymentEvent(BaseModel):
    type: str  # payment.succeeded, payment.failed...
    data: Payment


class NewEndpoint(BaseModel):
    url: HttpUrl


class CreatedEndpoint(BaseModel):
    id: str
    url: str
    secret: str  # shown once: the user checks the shop's signatures with it


class Delivery(BaseModel):
    event_id: str
    type: str
    outcome: str


@dataclass
class Endpoint:
    """A user's URL. A real database keeps its secret encrypted."""

    id: str
    url: str
    signer: WebhookSigner
    deliveries: list[Delivery] = field(default_factory=list)


@dataclass
class Shop:
    """Stands in for the database."""

    paid: dict[str, int] = field(default_factory=dict)
    # The provider's messages handled already: a unique constraint in a database, on a row
    # inserted in the same transaction as their effects.
    handled: set[str] = field(default_factory=set)
    endpoints: dict[str, Endpoint] = field(default_factory=dict)


def get_shop(request: Request) -> Shop:
    shop: Shop = request.app.state.shop
    return shop


def get_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.client
    return client


ShopDep = Annotated[Shop, Depends(get_shop)]
ClientDep = Annotated[httpx.AsyncClient, Depends(get_client)]
router = APIRouter()


async def post_event(
    client: httpx.AsyncClient,
    endpoint: Endpoint,
    event: dict[str, Any],
) -> httpx.Response:
    body = json.dumps(event).encode()  # signed as sent: json= would serialise it again
    headers = endpoint.signer.sign(body, message_id=event["id"])  # the same id on every retry
    headers["content-type"] = "application/json"
    return await client.post(endpoint.url, content=body, headers=headers)


async def deliver(client: httpx.AsyncClient, endpoint: Endpoint, event: dict[str, Any]) -> None:
    """Send `event` and log what happened. A real app retries failed deliveries later, from a
    task queue, with the same event id."""
    try:
        response = await post_event(client, endpoint, event)
        outcome = "delivered" if response.is_success else f"HTTP {response.status_code}"
    except CircuitOpenError:
        outcome = "skipped"  # it failed too often lately: not called for a minute
    except BlockedDestinationError:
        outcome = "blocked"  # its name points to a private address now
    except httpx.HTTPError as exc:  # can't connect, no answer in time, answer too large...
        outcome = type(exc).__name__
    endpoint.deliveries.append(Delivery(event_id=event["id"], type=event["type"], outcome=outcome))


async def notify(
    client: httpx.AsyncClient,
    endpoints: list[Endpoint],
    event: dict[str, Any],
) -> None:
    """Send `event` to every endpoint at once: they don't wait for each other."""
    await asyncio.gather(*(deliver(client, endpoint, event) for endpoint in endpoints))


@router.post("/webhooks/payments", status_code=status.HTTP_204_NO_CONTENT)
async def payment_event(
    message: Annotated[WebhookMessage, Depends(payment_webhooks)],
    shop: ShopDep,
    client: ClientDep,
    background: BackgroundTasks,
) -> None:
    """The provider's events. Every attempt of a message has the same id: the first one does the
    work, the others only get their 204."""
    if message.id in shop.handled:
        return
    # Parsed once its signature is checked: FastAPI would parse a body parameter before.
    try:
        event = PaymentEvent.model_validate_json(message.body)
    except ValidationError:
        raise HTTPException(422, "Not a payment event") from None
    shop.handled.add(message.id)
    if event.type == "payment.succeeded":
        shop.paid[event.data.order_id] = event.data.amount
        order_paid = {"id": f"evt_{secrets.token_hex(12)}", "type": "order.paid"}
        # After the response: the provider doesn't wait for the users' servers.
        background.add_task(
            notify, client, list(shop.endpoints.values()), order_paid | event.data.model_dump()
        )


@router.post("/endpoints", status_code=status.HTTP_201_CREATED)
async def add_endpoint(new: NewEndpoint, shop: ShopDep, client: ClientDep) -> CreatedEndpoint:
    """Register a URL for the shop's events, if it accepts a signed ping. A real app keeps each
    user's endpoints apart."""
    secret = WebhookSigner.generate_secret()
    endpoint = Endpoint(f"ep_{secrets.token_hex(8)}", str(new.url), WebhookSigner(secret))
    ping = {"id": f"evt_{secrets.token_hex(12)}", "type": "ping"}
    try:
        response = await post_event(client, endpoint, ping)
    except BlockedDestinationError as exc:  # says which host, never the address it resolved to
        raise HTTPException(422, f"Not allowed: {exc}") from None
    except httpx.HTTPError as exc:
        raise HTTPException(422, f"The ping failed: {type(exc).__name__}") from None
    if not response.is_success:
        raise HTTPException(422, f"The ping got HTTP {response.status_code}")
    shop.endpoints[endpoint.id] = endpoint
    return CreatedEndpoint(id=endpoint.id, url=endpoint.url, secret=secret)


@router.get("/endpoints/{endpoint_id}/deliveries")
async def deliveries(endpoint_id: str, shop: ShopDep) -> list[Delivery]:
    endpoint = shop.endpoints.get(endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "Endpoint not found")
    return endpoint.deliveries


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """The application. `transport` sends the outgoing requests once they're checked: the network by
    default, a mock in tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Redis or Valkey in production, so that every process refuses a replay. The marks are
        # counters, which a signing_key doesn't cover: keep the backend out of others' reach.
        FastAPICache.init(InMemoryBackend(), prefix="shop")
        # Only public addresses, on the web's ports, checked again on every call.
        guard = SSRFGuardTransport(transport, ports=(80, 443))
        outbound = ResilientTransport(
            guard, timeout=10, failure_threshold=5, reset_timeout=60, max_response_size=65_536
        )
        async with httpx.AsyncClient(transport=outbound) as client:  # it follows no redirects
            application.state.client = client
            application.state.shop = Shop()
            yield

    application = FastAPI(lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
