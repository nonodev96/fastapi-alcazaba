"""A team's shared documents, whose Cedar policies decide who reads, edits and deletes each one by
its attributes: its team, its owner, whether it's confidential, and who's asking.

    uvicorn examples.authz.cedar_policies:app

The members of a document's team read it, its owner edits it, admins delete documents, and
contractors never see a confidential one, whatever else allows it: a `forbid` beats every
`permit`. The policies are in policies.cedar, validated at start against schema.cedarschema.
The user (with its team and roles) is the entity the authentication gives; each document, the
entity the route builds from the row it read.
"""

from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from fastapi_alcazaba.authz import FastAPIAuthz
from fastapi_alcazaba.authz.cedar import CedarEnforcer

HERE = Path(__file__).parent


class Document(BaseModel):
    id: int
    title: str = Field(max_length=200)
    owner: str
    team: str
    confidential: bool = False


class Edit(BaseModel):
    title: str = Field(max_length=200)


class User(BaseModel):
    name: str
    team: str
    contractor: bool = False
    roles: list[str] = []


class Database:
    """Stands in for the database."""

    def __init__(self) -> None:
        self.documents = {
            1: Document(id=1, title="Style guide", owner="ana", team="editorial"),
            2: Document(
                id=2, title="Budget 2027", owner="ben", team="editorial", confidential=True
            ),
            3: Document(id=3, title="Sales targets", owner="dan", team="sales"),
        }


db = Database()
bearer = HTTPBearer(auto_error=False)
# Stands in for the app's authentication (JWTs, sessions, API keys): a token per user.
USERS = {
    "ana-token": User(name="ana", team="editorial", roles=["admin"]),
    "ben-token": User(name="ben", team="editorial"),
    "eva-token": User(name="eva", team="editorial", contractor=True),
    "dan-token": User(name="dan", team="sales"),
}


def user_entity(user: User) -> dict[str, Any]:
    """The user as Cedar sees it: its attributes, and its roles as parents."""
    return {
        "uid": {"type": "User", "id": user.name},
        "attrs": {"team": user.team, "contractor": user.contractor},
        "parents": [{"type": "Role", "id": role} for role in user.roles],
    }


def document_entity(document: Document) -> dict[str, Any]:
    owner = {"__entity": {"type": "User", "id": document.owner}}
    attrs = {"owner": owner, "team": document.team, "confidential": document.confidential}
    return {"uid": {"type": "Document", "id": str(document.id)}, "attrs": attrs, "parents": []}


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> dict[str, Any] | None:
    """Who makes the request, as a Cedar entity; None without a token."""
    if credentials is None:
        return None
    user = USERS.get(credentials.credentials)
    if user is None:
        raise HTTPException(401, "Invalid token", headers={"WWW-Authenticate": "Bearer"})
    return user_entity(user)


enforcer = CedarEnforcer(
    (HERE / "policies.cedar").read_text(),
    entities=[{"uid": {"type": "Role", "id": "admin"}, "attrs": {}, "parents": []}],
    schema=(HERE / "schema.cedarschema").read_text(),  # the policies are checked against it
)
authz = FastAPIAuthz(enforcer, subject=current_user)  # a 401 without a token
Member = Annotated[dict[str, Any], Depends(authz)]  # (user, path, method): GET /documents


async def signed_in(
    user: Annotated[dict[str, Any] | None, Depends(current_user)],
) -> dict[str, Any]:
    """The user, for routes that ask the policy about the document they read."""
    if user is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


SignedIn = Annotated[dict[str, Any], Depends(signed_in)]

app = FastAPI()


async def readable(user: dict[str, Any], document_id: int) -> Document:
    """The document, if the user may read it; a 404 otherwise, as if it didn't exist."""
    document = db.documents.get(document_id)
    if document is None or not await authz.enforce(user, document_entity(document), "read"):
        raise HTTPException(404, "No such document")
    return document


@app.get("/documents")
async def list_documents(user: Member) -> list[Document]:
    """Those the user may read: the policy decides for each one."""
    documents = db.documents.values()
    return [d for d in documents if await authz.enforce(user, document_entity(d), "read")]


@app.get("/documents/{document_id}")
async def read_document(document_id: int, user: SignedIn) -> Document:
    return await readable(user, document_id)


@app.put("/documents/{document_id}")
async def edit_document(document_id: int, edit: Edit, user: SignedIn) -> Document:
    document = await readable(user, document_id)
    if not await authz.enforce(user, document_entity(document), "edit"):
        raise HTTPException(403, "Only its owner edits a document")
    document.title = edit.title
    return document


@app.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(authz.require("documents", "delete"))],
)
async def delete_document(document_id: int) -> None:
    """A named action, not a path: (user, documents, delete)."""
    if db.documents.pop(document_id, None) is None:
        raise HTTPException(404, "No such document")


if __name__ == "__main__":
    uvicorn.run("cedar_policies:app", reload=True)
