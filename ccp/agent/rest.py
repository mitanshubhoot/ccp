"""The REST surface, generated from the tool registry (spec.md A-117).

    GET  /v1/tools          the tools the caller's principal may call, each its whole contract
    POST /v1/tools/{name}   one call: the body is the tool's arguments, the answer its output,
                            both under the registry's own schemas

A REST call takes the same path as an agent's: the gateway, `policy.check` under the caller's
principal (`api:<role>`), the read-only role, the call limits, the output check, every decision
logged with the caller's user. The gateway's answer maps onto HTTP: 200 a result, 403 refused
by policy (an `ask` included), 404 an unknown tool or data that is missing or not found, 422
arguments the schema refuses, 429 a call limit, 500 a malformed result. `openapi()` is the
document. Nothing here listens: MT4 serves it behind sign-in, with the caller's role from the
identity provider; until then no HTTP server exists. No MCP server in v1 (the answer to U12);
a read-only one would be generated from the same registry when customers' agents ask.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ccp.agent.authz import Decision, Gate, Rule
from ccp.agent.bundle import bundle_sha256
from ccp.agent.tools import TOOL_SPECS, ToolContext, bind_tools
from ccp.evaluators.tier2 import Providers
from ccp.storage.scoped import ScopedDB

_CALL = re.compile(r"/v1/tools/([a-z_]+)")
_REF = "#/components/schemas/{model}"


@dataclass(frozen=True)
class Caller:
    """Who calls: the tenant comes from the session, never from the request (MT4)."""

    role: str
    user_id: str

    @property
    def principal(self) -> str:
        return f"api:{self.role}"


class _Error(BaseModel):
    error: str
    missing: str | None = None
    policy: str | None = None


def _schema(model: type[BaseModel], components: dict[str, Any]) -> dict[str, Any]:
    """The model's JSON schema with its nested definitions hoisted into the components."""
    schema = model.model_json_schema(ref_template=_REF)
    components.update(schema.pop("$defs", {}))
    components[model.__name__] = schema
    return {"$ref": _REF.format(model=model.__name__)}


def openapi() -> dict[str, Any]:
    components: dict[str, Any] = {}
    error = _schema(_Error, components)
    errors = {
        str(code): {"description": text, "content": {"application/json": {"schema": error}}}
        for code, text in (
            (403, "refused by policy.check"),
            (404, "unknown tool, missing data, or not found"),
            (422, "arguments the schema refuses"),
            (429, "the tool's per-run call limit"),
            (500, "the tool returned a malformed result"),
        )
    }
    paths: dict[str, Any] = {
        "/v1/tools": {
            "get": {
                "operationId": "list_tools",
                "summary": "The tools the caller may call",
                "responses": {"200": {"description": "each tool's whole contract"}},
            }
        }
    }
    for name in sorted(TOOL_SPECS):
        spec = TOOL_SPECS[name]
        paths[f"/v1/tools/{name}"] = {
            "post": {
                "operationId": name,
                "summary": spec.description,
                "x-ccp-class": spec.cls,
                "x-ccp-scopes": list(spec.scopes),
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": _schema(spec.args, components)}},
                },
                "responses": {
                    "200": {
                        "description": "the tool's result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"result": _schema(spec.output, components)},
                                    "required": ["result"],
                                }
                            }
                        },
                    },
                    **errors,
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "CCP tools", "version": bundle_sha256()[:12]},
        "paths": paths,
        "components": {
            "schemas": components,
            "securitySchemes": {"session": {"type": "http", "scheme": "bearer"}},
        },
        "security": [{"session": []}],
    }


def _status(result: dict[str, Any]) -> int:
    if not result.get("is_error"):
        return 200
    if result.get("denied") or result.get("policy"):
        return 403
    if result.get("rate_limited"):
        return 429
    if result.get("malformed_result"):
        return 500
    if result.get("missing") or result.get("error") == "not found":
        return 404
    return 422  # invalid arguments, or a pinned argument changed


def handle(
    caller: Caller,
    method: str,
    path: str,
    body: bytes | None,
    *,
    log_db: ScopedDB,
    read_db: ScopedDB,
    providers: Providers,
    table: tuple[Rule, ...] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One request, answered in-process. `log_db` (the application role) holds the decision
    log; `read_db` (the read-only role) is what the tools see."""
    gate = Gate(caller.principal, user_id=caller.user_id, log_db=log_db, table=table)
    if method == "GET" and path == "/v1/tools":
        return 200, {"tools": [TOOL_SPECS[n].contract() for n in gate.offered(sorted(TOOL_SPECS))]}
    match = _CALL.fullmatch(path)
    if match is None:
        return 404, {"error": f"no route {method} {path}"}
    if method != "POST":
        return 405, {"error": f"{path} takes POST"}
    name = match.group(1)
    if name not in TOOL_SPECS:
        return 404, {"error": f"no tool {name!r}"}
    try:
        args = json.loads(body or b"{}")
    except ValueError:
        return 422, {"error": "the body is not JSON"}
    if not isinstance(args, dict):
        return 422, {"error": "the body must be an object of arguments"}
    ctx = ToolContext(read_db, providers.embedder, providers.reranker, providers.retrieval)
    tools = bind_tools(ctx, (name,), gate=gate)
    if name not in tools:  # no enforced rule could permit it: the check says why, and logs
        decision = gate.decide(name, TOOL_SPECS[name].cls, args)  # a watched rule is named
        if decision.permitted:  # cannot happen: offered() covers every enforced permit
            decision = gate.record(
                name, TOOL_SPECS[name].cls, args, Decision("deny", None, "not offered")
            )
        return 403, {"error": f"not permitted: {decision.reason}", "policy": decision.effect}
    result = tools[name].call(args)
    status = _status(result)
    if status == 200:
        return 200, {"result": result["result"]}
    return status, {
        "error": str(result.get("error")),
        "missing": result.get("missing"),
        "policy": result.get("policy") or ("deny" if result.get("denied") else None),
    }
