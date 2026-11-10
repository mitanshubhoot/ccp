"""The release bundle hash (spec.md A-100): one digest over everything that shapes what a model
is asked and how its tools answer it. A change to any part is a new bundle, so an eval run,
a model call and a replay can say exactly which bundle they ran.

    prompts       every prompt file under ccp/llm/prompts/, by name and content
    tools         every tool's whole declaration: what the model sees, its output schema,
                  class, scopes, limits and version (A-112)
    tool errors   the fixed error texts a tool or the loop returns to the model
    allowlists    which graph may call which tool
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

from ccp.agent import authz
from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.tools import TOOL_SPECS
from ccp.llm.prompts import PROMPTS_DIR

# The error texts a model can read in a tool result; each is part of the bundle.
TOOL_ERRORS: dict[str, str] = {
    "not_permitted": "tool not permitted in this graph",
    "fixed_argument": "argument {key} is fixed to {value!r} in this graph",
    "invalid_arguments": "invalid arguments: {message}",
    "not_found": "not found",
    "withheld": "result withheld: it carried the fence marker",
    "missing": "no {what} ... (the data the tool needs is absent)",
    "rate_limited": "{tool} may be called at most {n} times in a run",
    "malformed_result": "{tool} returned a malformed result",
    "truncated": (
        "[TRUNCATED: {omitted} of {total} characters not shown; this result is incomplete]"
    ),
}


def parts() -> list[str]:
    out: list[str] = []
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        out.append(f"prompt {path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}")
    for name in sorted(TOOL_SPECS):
        out.append(f"tool {name} {json.dumps(TOOL_SPECS[name].contract(), sort_keys=True)}")
    for key in sorted(TOOL_ERRORS):
        out.append(f"tool_error {key} {TOOL_ERRORS[key]}")
    for graph in sorted(ALLOWLISTS):
        out.append(f"allowlist {graph} {sorted(ALLOWLISTS[graph])}")
    try:  # who may call what is part of what a run is (A-113)
        table = [r.model_dump(mode="json") for r in authz.rules()]
    except Exception as exc:  # a broken rule file denies every call; the bundle says so
        table = [{"error": f"{type(exc).__name__}"}]
    out.append(f"policy {json.dumps(table, sort_keys=True)}")
    return out


@lru_cache(maxsize=1)
def bundle_sha256() -> str:
    return hashlib.sha256("\n".join(parts()).encode("utf-8")).hexdigest()
