"""A newsroom API whose policy, not its routes, says who may do what: Casbin's RBAC.

    uvicorn examples.authz.main:app

Anybody reads the published articles; writers write drafts, editors edit and publish them, and
admins give people roles. Who may do what is in policy.csv (`p` lines: what a role may do; `g`
lines: who has which role, and which roles include others), checked by FastAPICasbin: on the path
and method of a route (`Depends(authz)`), or on a named action (`Depends(authz.require(...))`).

The policy is read from files here; in production, from a database through one of Casbin's
async adapters (casbin-async-sqlalchemy-adapter...), which also keeps the roles granted at runtime.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import casbin
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from fastapi_alcazaba import FastAPICasbin

HERE = Path(__file__).parent


class Role(StrEnum):
    # An enum: admins can only grant the roles the policy knows.
    WRITER = "writer"
    EDITOR = "editor"
    ADMIN = "admin"


class Draft(BaseModel):
    title: str = Field(max_length=200)
    body: str = Field(max_length=10_000)


class Article(Draft):
    id: int
    author: str
    published_by: str | None = None


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.articles = {
            1: Article(
                id=1, title="Olive harvest starts", body="...", author="cris", published_by="ben"
            ),
            2: Article(id=2, title="Draft: new mill", body="...", author="cris"),
        }


# Async: its changes to the policy (granting a role) run in the event loop, like the checks.
enforcer = casbin.AsyncEnforcer(str(HERE / "model.conf"), str(HERE / "policy.csv"))
db = Database()
bearer = HTTPBearer(auto_error=False)
# Stands in for the app's authentication (JWTs, sessions, API keys): a token per user.
TOKENS = {"ana-token": "ana", "ben-token": "ben", "cris-token": "cris", "dan-token": "dan"}


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str | None:
    """Who makes the request; None for a visitor without a token."""
    if credentials is None:
        return None
    user = TOKENS.get(credentials.credentials)
    if user is None:  # a wrong token isn't a visitor without one
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return user


# Routes open to visitors when the policy says so: the policy knows them as "anonymous".
authz = FastAPICasbin(enforcer, subject=current_user, anonymous="anonymous")
# Routes for people with an account: a 401 without one, before asking the policy.
members = FastAPICasbin(enforcer, subject=current_user)
Visitor = Annotated[str | None, Depends(authz)]
Member = Annotated[str, Depends(members)]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await enforcer.load_policy()  # an AsyncEnforcer reads it when told
    yield


app = FastAPI(lifespan=lifespan)


def reads_drafts(user: str | None) -> bool:
    # A check inside the route: the route is open to everybody, the drafts aren't.
    return user is not None and authz.enforce(user, "drafts", "read")


@app.get("/articles")
async def list_articles(user: Visitor) -> list[Article]:
    """The published articles; the drafts too for those who may read them."""
    drafts = reads_drafts(user)
    return [article for article in db.articles.values() if article.published_by or drafts]


@app.get("/articles/{article_id}")
async def read_article(article_id: int, user: Visitor) -> Article:
    article = db.articles.get(article_id)
    if article is None or not (article.published_by or reads_drafts(user)):
        raise HTTPException(404, "No such article")  # for readers, a draft doesn't exist
    return article


@app.post("/articles", status_code=status.HTTP_201_CREATED)
async def write_article(draft: Draft, user: Member) -> Article:
    article = Article(id=max(db.articles) + 1, author=user, **draft.model_dump())
    db.articles[article.id] = article
    return article


@app.put("/articles/{article_id}")
async def edit_article(article_id: int, draft: Draft, user: Member) -> Article:
    article = db.articles.get(article_id)
    if article is None:
        raise HTTPException(404, "No such article")
    if article.author != user and not authz.enforce(user, "articles", "edit"):
        raise HTTPException(403, "Only its author or an editor edits an article")
    article.title, article.body = draft.title, draft.body
    return article


@app.post("/articles/{article_id}/publish")
async def publish_article(
    article_id: int,
    editor: Annotated[str, Depends(members.require("articles", "publish"))],
) -> Article:
    """A named action, not a path: (editor, articles, publish)."""
    article = db.articles.get(article_id)
    if article is None:
        raise HTTPException(404, "No such article")
    article.published_by = editor
    return article


@app.put(
    "/users/{user}/roles/{role}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(members.require("roles", "grant"))],
)
async def grant_role(user: str, role: Role) -> None:
    """Takes effect at once in this process. The other processes see it when they load the policy
    again: a Casbin watcher (casbin-redis-watcher...) tells them."""
    await enforcer.add_role_for_user(user, role)


if __name__ == "__main__":
    uvicorn.run("main:app", reload=True)
