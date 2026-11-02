"""Cedar policies for `FastAPIAuthz` and `AuthzMiddleware`: the engine of
[Cedar](https://www.cedarpolicy.com), in the process, through cedarpy
(`pip install "fastapi-alcazaba[cedar]"`).

    POLICIES = '''
    permit(principal in Role::"editor", action == Action::"publish", resource == Resource::"news");
    permit(principal, action == Action::"GET", resource) when { resource.id like "/news/*" };
    '''
    enforcer = CedarEnforcer(POLICIES, entities=ROLES, entities_of=user_entities)
    authz = FastAPIAuthz(enforcer, subject=current_username)

The requests of `FastAPIAuthz`, (subject, object, action), become Cedar's (principal, action,
resource): `User::"ana"`, `Action::"PUT"`, `Resource::"/articles/7"`. Each resource is an entity
with an `id` attribute, its name, so that a policy can match paths with `like`.
"""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import cedarpy

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: An entity of Cedar's JSON format: `{"uid": {"type": "User", "id": "ana"}, "attrs": {...},
#: "parents": [{"type": "Role", "id": "editor"}]}`.
Entity = Mapping[str, Any]


class CedarEnforcer:
    """An enforcer of Cedar policies, for `FastAPIAuthz(enforcer)` and `AuthzMiddleware`.

    `await enforce(subject, resource, action, context=None)` asks Cedar whether `subject` may do
    `action` on `resource`. Cedar denies what no policy permits, and a `forbid` beats every
    `permit`. A policy that fails to evaluate (an attribute that isn't there, an entity that
    doesn't exist) refuses the request and logs why: Cedar would skip it, and a `forbid` that
    errors would let the request through.

    :param policies: the policies, in Cedar's language. Invalid ones raise ValueError here.
    :param entities: the entities every request sees: roles and their hierarchy, resources with
        their attributes.
    :param entities_of: `entities_of(subject)`: the entities of one request, such as the user
        and its roles from your database. A plain function, for what's at hand (the user the
        authentication already loaded), or a coroutine function, awaited, for what it reads
        through the network.
    :param schema: a Cedar schema (its language, or JSON): the policies are validated against
        it, and the entities checked.
    :param principal_type: the entity type of subjects given as strings.
    :param action_type: the entity type of actions.
    :param resource_type: the entity type of resources given as strings, paths too.
    """

    def __init__(
        self,
        policies: str,
        *,
        entities: Sequence[Entity] = (),
        entities_of: Callable[[Any], Sequence[Entity] | Awaitable[Sequence[Entity]]] | None = None,
        schema: str | Mapping[str, Any] | None = None,
        principal_type: str = "User",
        action_type: str = "Action",
        resource_type: str = "Resource",
    ) -> None:
        self._schema = _schema(schema)
        try:
            self._policies = cedarpy.PolicySet.from_str(policies)
        except ValueError as exc:
            raise ValueError(f"invalid Cedar policies: {exc}") from None
        if self._schema is not None:
            validated = cedarpy.validate_policies(policies, self._schema)
            if not validated.validation_passed:
                errors = "; ".join(str(error) for error in validated.errors)
                raise ValueError(f"the Cedar policies don't fit the schema: {errors}")
        self._entities = cedarpy.Entities.from_json_str(json.dumps(list(entities)), self._schema)
        self._known = {_uid(entity) for entity in entities}
        self.entities_of = entities_of
        self.principal_type = principal_type
        self.action_type = action_type
        self.resource_type = resource_type

    async def enforce(
        self,
        subject: Any,
        resource: Any,
        action: Any,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """Whether the policies let `subject` do `action` on `resource`.

        `subject` and `resource` are each a name (of `principal_type` or `resource_type`), an
        entity uid (`{"type": "Team", "id": "core"}`), or a whole entity with its attributes
        (`{"uid": ..., "attrs": {"owner": ...}, "parents": [...]}`), which this request sees:
        the user your authentication loaded, the document the route just read. `action` is a
        name of `action_type` or a uid. `context` is Cedar's `context`.

        An entity may come more than once (from `entities`, `entities_of` and the arguments) only
        the same each time: one that differs raises ValueError rather than pick either."""
        principal, principal_entity = _split(subject, self.principal_type)
        target, target_entity = _split(resource, self.resource_type)
        extra = await self._entities_of(subject)
        extra += [entity for entity in (principal_entity, target_entity) if entity is not None]
        if (target["type"], target["id"]) not in self._known | {_uid(e) for e in extra}:
            extra.append({"uid": target, "attrs": {"id": target["id"]}, "parents": []})
        try:
            entities = self._entities.with_added_json_str(json.dumps(extra), self._schema)
        except ValueError as exc:
            raise ValueError(f"the entities of the request don't fit: {exc}") from None
        request = {
            "principal": principal,
            "action": _reference(action, self.action_type),
            "resource": target,
            "context": dict(context or {}),
        }
        result = cedarpy.is_authorized(request, self._policies, entities, self._schema)
        if result.diagnostics.errors:
            logger.warning(
                "Refused: Cedar couldn't evaluate every policy: %s",
                "; ".join(result.diagnostics.errors),
            )
            return False
        return bool(result.allowed)

    async def _entities_of(self, subject: Any) -> list[Entity]:
        if self.entities_of is None:
            return []
        entities = self.entities_of(subject)
        if inspect.isawaitable(entities):
            entities = await entities
        return list(entities)


def _schema(schema: str | Mapping[str, Any] | None) -> Any:
    if schema is None:
        return None
    try:
        if isinstance(schema, str):
            return cedarpy.Schema.from_str(schema)
        return cedarpy.Schema.from_json_str(json.dumps(schema))
    except ValueError as exc:
        raise ValueError(f"invalid Cedar schema: {exc}") from None


def _split(value: Any, kind: str) -> tuple[dict[str, str], Entity | None]:
    """The uid of `value`, and the entity when it's a whole one."""
    if isinstance(value, Mapping) and "uid" in value:
        return _reference(value["uid"], kind), value
    return _reference(value, kind), None


def _reference(value: Any, kind: str) -> dict[str, str]:
    """The uid of `value`: a name, of type `kind`, or a uid already."""
    if isinstance(value, str):
        return {"type": kind, "id": value}
    if isinstance(value, Mapping) and isinstance(value.get("type"), str):
        if isinstance(value.get("id"), str) and value.keys() == {"type", "id"}:
            return {"type": value["type"], "id": value["id"]}
    raise TypeError(f"give a name, {{'type': ..., 'id': ...}} or an entity, not {value!r}")


def _uid(entity: Entity) -> tuple[str, str]:
    uid = _reference(entity.get("uid"), "")
    return uid["type"], uid["id"]
