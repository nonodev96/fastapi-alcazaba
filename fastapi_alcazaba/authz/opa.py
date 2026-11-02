"""Open Policy Agent's decisions for `FastAPIAuthz` and `AuthzMiddleware`: the policy decision
point that runs as a service of its own, asked through its REST API
(`pip install "fastapi-alcazaba[opa]"`).

    enforcer = OPAEnforcer("http://localhost:8181", rule="authz.allow")
    authz = FastAPIAuthz(enforcer, subject=current_user)

Each check is a query of the rule, `POST /v1/data/authz/allow`, with the request as its input:
`{"subject": ..., "resource": ..., "action": ..., "context": {...}}`. Only a result of `true`
lets the request through. A service that doesn't answer in time, or answers with an error,
raises `PolicyUnavailable`: a 503, never a request let through.
"""

import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from fastapi_alcazaba.authz.enforcer import PolicyUnavailable

try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover  # httpx under its old name
    try:
        import httpx  # type: ignore[no-redef,import-not-found]
    except ImportError as exc:
        raise ImportError('OPAEnforcer needs httpx2: pip install "fastapi-alcazaba[opa]"') from exc

from fastapi_alcazaba.outbound.transports import ResilientTransport

logger: logging.Logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# The reference of a rule, as Rego writes it after `data.`: `authz.allow`, `app.rbac.allow`.
_RULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
# A decision is a few bytes: more is something else answering.
_MAX_RESPONSE = 64 * 1024


class OPAEnforcer:
    """An enforcer that asks Open Policy Agent, for `FastAPIAuthz(enforcer)` and
    `AuthzMiddleware`.

    `await enforce(subject, resource, action, context=None)` queries `rule` with the input
    `{"subject": subject, "resource": resource, "action": action, "context": context}`: the
    subject your dependency gives (a name, or a dict with the user's attributes and roles), the
    path or the value of `authz.require`, and the method or the action. Only a result of `true`
    allows; `false` refuses, and so does a rule that is undefined for the input (with a warning:
    give it a default, `default allow := false`).

    Fails closed: a service that doesn't answer within `timeout`, or answers with a 5xx, raises
    `PolicyUnavailable`, and the request gets a 503. Its own client gives each call a deadline
    and the service a circuit breaker (`ResilientTransport`): when it's down, requests are refused
    at once instead of each waiting for it. A query OPA refuses (a 4xx: a wrong token, an input
    that isn't JSON) and a result that isn't a boolean (a `rule` that names a package, or a rule
    that gives an object) raise RuntimeError, a 500: they're mistakes to fix, not refusals.

    :param url: where OPA listens, `http://localhost:8181` for a sidecar.
    :param rule: the rule to query, as Rego references it after `data.`: `authz.allow` is the
        rule `allow` of `package authz`.
    :param token: the bearer token OPA asks for, when it runs with `--authentication=token`.
    :param timeout: seconds for each query, the answer included.
    :param client: the httpx2 client to ask OPA with (for its TLS, its proxy, a transport of your
        own); one of its own by default, with a `ResilientTransport`. `aclose()` closes only its
        own.
    """

    def __init__(
        self,
        url: str,
        *,
        rule: str = "authz.allow",
        token: str | SecretStr | None = None,
        timeout: float = 1.0,
        client: "httpx.AsyncClient | None" = None,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"url is OPA's http:// or https:// address, not {url!r}")
        if parts.query or parts.fragment:
            raise ValueError(f"url is OPA's address, without a query or a fragment: {url!r}")
        if not _RULE.fullmatch(rule):
            raise ValueError(f"rule is a reference such as authz.allow, not {rule!r}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.rule = rule
        self.query_url = f"{url.rstrip('/')}/v1/data/{rule.replace('.', '/')}"
        self.token = SecretStr(token) if isinstance(token, str) else token
        if self.token is not None and not self.token.get_secret_value():
            raise ValueError("token is empty: give OPA's token, or None")
        self.timeout = timeout
        self._own_client = client is None
        self.client = (
            client
            if client is not None
            else httpx.AsyncClient(
                transport=ResilientTransport(timeout=timeout, max_response_size=_MAX_RESPONSE)
            )
        )

    async def enforce(
        self,
        subject: Any,
        resource: Any,
        action: Any,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """Whether `rule` is true for the input (subject, resource, action, context). Every value
        goes to OPA as JSON: names, numbers, lists and dicts of them."""
        query = {
            "input": {
                "subject": subject,
                "resource": resource,
                "action": action,
                "context": dict(context or {}),
            }
        }
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        try:
            response = await self.client.post(
                self.query_url, json=query, headers=headers, timeout=self.timeout
            )
        except httpx.TransportError as exc:  # refused, timed out, its circuit breaker open
            raise PolicyUnavailable(f"Open Policy Agent didn't answer: {exc!r}") from exc
        if response.status_code >= 500:
            raise PolicyUnavailable(
                f"Open Policy Agent answered {response.status_code}: {_message(response)}"
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Open Policy Agent refused the query of {self.rule} "
                f"({response.status_code}): {_message(response)}"
            )
        try:
            decision = response.json()
        except ValueError:
            decision = None
        if not isinstance(decision, dict):
            raise RuntimeError(f"{self.query_url} didn't answer as Open Policy Agent does")
        if "result" not in decision:
            logger.warning(
                "Refused: %s is undefined for this input; give it a default "
                "(default allow := false), or check that the rule exists",
                self.rule,
            )
            return False
        result = decision["result"]
        if not isinstance(result, bool):
            raise RuntimeError(
                f"{self.rule} gives {type(result).__name__}, not a boolean: "
                "rule= names a rule that is true or false"
            )
        if not result:
            logger.debug("%s refused it (decision %s)", self.rule, decision.get("decision_id"))
        return result

    async def aclose(self) -> None:
        """Closes the client it made (not one it was given): in the lifespan, at shutdown."""
        if self._own_client:
            await self.client.aclose()


def _message(response: "httpx.Response") -> str:
    """OPA's error message (`{"code": ..., "message": ...}`), or the start of the body."""
    try:
        error = response.json()
    except ValueError:
        return response.text[:200]
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else json.dumps(error)[:200]
