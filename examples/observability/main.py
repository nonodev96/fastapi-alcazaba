"""An orders API that is easy to operate: errors, request IDs, deadlines and health checks.

    python examples/observability/main.py  # uvicorn, with the logging configuration below

- Errors are RFC 9457 problem details (`application/problem+json`): a validation error says where
  and what, never the values sent; running out of stock is a `Problem` with an `available`
  member; a bug is a bare 500 that tells the client nothing about it.
- Every request has an ID, in the `X-Request-ID` response header, in every problem and in every
  log line, uvicorn's included: a client quotes it, and its lines are found in the logs.
- Every request has a deadline: the slow sales report gets a 504 and its database connection
  back, while the CSV export streams for as long as it takes.
- `/health/live` and `/health/ready` for the load balancer: the database is a critical check, the
  cache a non-critical one.

OpenTelemetry needs nothing here. Run the app under its zero-code instrumentation
(`opentelemetry-instrument`, see "Metrics and traces" in the guide): the FastAPI instrumentation
traces each request, and Alcazaba's metrics (problems, request_timeouts, request_ids,
health.checks...) go out through the meter of the app's cache.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from fastapi_alcazaba import Cache, FastAPIProblemDetails, Problem
from fastapi_alcazaba.backends.inmemory import InMemoryBackend
from fastapi_alcazaba.health import health_router
from fastapi_alcazaba.request_id import RequestIDMiddleware
from fastapi_alcazaba.timeouts import RequestTimeoutMiddleware

logger = logging.getLogger("orders")

LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
ACCESS_FORMAT = '%(asctime)s %(levelname)s [%(request_id)s] "%(request_line)s" %(status_code)s'
# uvicorn's log_config. The filter goes on every handler (a logger's filters don't see the records
# of its children), so uvicorn's access and error lines carry the ID too. Plain JSON: for the
# uvicorn command, save it to a file and pass --log-config.
LOGGING: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "fastapi_alcazaba.request_id.RequestIDLogFilter"}},
    "formatters": {
        "default": {"format": LOG_FORMAT},
        "access": {"()": "uvicorn.logging.AccessFormatter", "fmt": ACCESS_FORMAT},
    },
    "handlers": {
        name: {"class": "logging.StreamHandler", "formatter": name, "filters": ["request_id"]}
        for name in ("default", "access")
    },
    "loggers": {"uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False}},
    "root": {"handlers": ["default"], "level": "INFO"},
}


class NewOrder(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=100)


class Order(NewOrder):
    id: int


class Database:
    """Stands in for the database and its connection pool."""

    def __init__(self) -> None:
        self.stock = {1: 25, 2: 3}  # units left, by product
        self.orders = [Order(id=n, product_id=1, quantity=1) for n in range(1, 31)]
        self.in_use = 0  # connections taken from the pool
        self.up = True
        self.report_seconds = 30.0  # the sales report is a slow query
        self.row_seconds = 0.5  # and the export reads its rows slowly

    async def ping(self) -> None:
        if not self.up:
            # What drivers say: a host and a port that a public probe mustn't show anyone.
            raise ConnectionRefusedError("db.internal:5432: connection refused")


async def connection(request: Request) -> AsyncIterator[Database]:
    """A connection from the pool, given back when the request ends, also when it times out."""
    db: Database = request.app.state.db
    db.in_use += 1
    try:
        yield db
    finally:
        db.in_use -= 1


DB = Annotated[Database, Depends(connection)]
router = APIRouter()


@router.post("/orders", status_code=status.HTTP_201_CREATED)
async def place_order(new: NewOrder, db: DB) -> Order:
    if new.product_id not in db.stock:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such product")  # a 4xx's detail is sent
    available = db.stock[new.product_id]
    if new.quantity > available:
        raise Problem(
            status.HTTP_409_CONFLICT,
            f"Only {available} left",
            type="https://example.com/problems/out-of-stock",
            title="Out of stock",
            extensions={"available": available},
        )
    db.stock[new.product_id] -= new.quantity
    order = Order(id=len(db.orders) + 1, **new.model_dump())
    db.orders.append(order)
    logger.info("Order %d placed", order.id)  # the filter adds the request's ID
    return order


@router.get("/orders/export")
async def export_orders(db: DB) -> StreamingResponse:
    """Every order as CSV, sent as the rows are read. It has no deadline (see `paths` below): once
    a response has started it can't become a 504, it would only be cut short."""

    async def rows() -> AsyncIterator[str]:
        yield "id,product_id,quantity\n"
        for order in list(db.orders):
            await asyncio.sleep(db.row_seconds)
            yield f"{order.id},{order.product_id},{order.quantity}\n"

    return StreamingResponse(rows(), media_type="text/csv")


@router.get("/orders/{order_id}/invoice")
async def invoice(order_id: int) -> dict[str, str]:
    """A failure nobody handled: the client gets a bare 500 with the request ID to quote, and the
    server logs the traceback under that ID."""
    raise ConnectionError(f"pdf-renderer.internal:9000 refused the invoice of order {order_id}")


@router.get("/reports/sales")
async def sales_report(db: DB) -> dict[str, int]:
    """At the deadline the query is cancelled, the connection goes back to the pool and the
    client gets a 504, instead of both waiting for as long as the query takes."""
    await asyncio.sleep(db.report_seconds)
    return {"orders": len(db.orders)}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    application.state.db = Database()
    yield


def create_app(*, request_timeout: float = 10.0) -> FastAPI:
    """The application, with a deadline of `request_timeout` seconds per request."""
    application = FastAPI(lifespan=lifespan)
    # In memory: nothing but this process reads or writes it. Alcazaba's metrics are recorded
    # through it, and /health/ready checks it.
    Cache(InMemoryBackend(), prefix="orders").install(application)
    FastAPIProblemDetails().install(application)
    application.include_router(router)

    async def database() -> None:
        await application.state.db.ping()

    application.include_router(health_router({"database": database}))
    # The last middleware added is the outermost. RequestIDMiddleware goes around the deadline, so
    # that the 504 it answers carries the ID too. (A 500 is answered outside every middleware;
    # FastAPIProblemDetails adds the ID to it itself.)
    application.add_middleware(
        RequestTimeoutMiddleware, timeout=request_timeout, paths={"/orders/export": None}
    )
    # Keeps a safe X-Request-ID set by the proxy (or the client); trust_incoming=False if clients
    # reach the application directly and shouldn't choose their IDs.
    application.add_middleware(RequestIDMiddleware)
    return application


app = create_app()

if __name__ == "__main__":
    uvicorn.run("main:app", reload=True, log_config=LOGGING)
