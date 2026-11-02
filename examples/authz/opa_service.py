"""A shop whose orders and refunds Open Policy Agent decides, asked over the network: the policy
lives in its own service, shared by every app of the company.

    docker run -p 8181:8181 -v "$PWD/examples/authz:/policies:ro" openpolicyagent/opa:1.21.4 \\
        run --server --addr 0.0.0.0:8181 /policies/shop.rego
    uvicorn examples.authz.opa_service:app

Everybody reads the catalogue, customers their own orders and the support staff every order.
Support refunds up to 100 euros, and a manager above that. The policy is in shop.rego: the app
sends who asks (with the user's roles), the order and the action, and OPA answers. When OPA
doesn't answer, the requests get a 503: nothing is let through.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fastapi_alcazaba.authz import FastAPIAuthz
from fastapi_alcazaba.authz.opa import OPAEnforcer


class Settings(BaseSettings):
    """The configuration, from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    opa_url: str = "http://localhost:8181"
    opa_token: SecretStr | None = None  # when OPA runs with --authentication=token


class Product(BaseModel):
    id: int
    name: str
    price: float


class Order(BaseModel):
    id: int
    customer: str
    total: float
    refunded: bool = False


class User(BaseModel):
    name: str
    roles: list[str] = []


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.products = [Product(id=1, name="Olive oil", price=9.5)]
        self.orders = {
            1: Order(id=1, customer="ana", total=40),
            2: Order(id=2, customer="ana", total=250),
            3: Order(id=3, customer="leo", total=15),
        }


settings = Settings()
db = Database()
bearer = HTTPBearer(auto_error=False)
# Stands in for the app's authentication (JWTs, sessions, API keys): a token per user.
USERS = {
    "ana-token": User(name="ana"),
    "leo-token": User(name="leo"),
    "sam-token": User(name="sam", roles=["support"]),
    "max-token": User(name="max", roles=["support", "manager"]),
}


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict[str, Any] | None:
    """Who makes the request, with what the policy reads of them; None without a token."""
    if credentials is None:
        return None
    user = USERS.get(credentials.credentials)
    if user is None:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return user.model_dump()


enforcer = OPAEnforcer(settings.opa_url, rule="shop.allow", token=settings.opa_token)
authz = FastAPIAuthz(enforcer, subject=current_user, anonymous="*anonymous")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await enforcer.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/products", dependencies=[Depends(authz)])
async def list_products() -> list[Product]:
    """(subject, path, method): the policy opens it to everybody, anonymous visitors too."""
    return db.products


async def signed_in(
    user: Annotated[dict[str, Any] | None, Depends(current_user)],
) -> dict[str, Any]:
    """The user, for routes that ask the policy about the order they read."""
    if user is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


SignedIn = Annotated[dict[str, Any], Depends(signed_in)]


@app.get("/orders/{order_id}")
async def read_order(order_id: int, user: SignedIn) -> Order:
    """The order, as OPA reads it: a 404 for those who may not, as if it didn't exist."""
    order = db.orders.get(order_id)
    if order is None or not await authz.enforce(user, order.model_dump(), "read"):
        raise HTTPException(404, "No such order")
    return order


@app.post("/orders/{order_id}/refund")
async def refund_order(order_id: int, user: SignedIn) -> Order:
    """The policy reads the order's total: support refunds small orders, managers any."""
    order = db.orders.get(order_id)
    if order is None or not await authz.enforce(user, order.model_dump(), "read"):
        raise HTTPException(404, "No such order")
    if not await authz.enforce(user, order.model_dump(), "refund"):
        raise HTTPException(403, "Refunds are for the support staff, and a manager above 100 euros")
    order.refunded = True
    return order


if __name__ == "__main__":
    uvicorn.run("opa_service:app", reload=True)
