"""The release bundle hash (spec.md A-100): one digest over everything that shapes what a model
is asked and how its tools answer it. A change to any part is a new bundle, so an eval run,
a model call and a replay can say exactly which bundle they ran.

    prompts       every prompt file under ccp/llm/prompts/, by name and content
    tools         every tool's name, description and argument schema, as the model sees it
    tool errors   the fixed error texts a tool or the loop returns to the model
    allowlists    which graph may call which tool
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache

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
    "truncated": (
        "[TRUNCATED: {omitted} of {total} characters not shown; this result is incomplete]"
    ),
}


def parts() -> list[str]:
    out: list[str] = []
    for path in sorted(PROMPTS_DIR.glob("*.md")):
        out.append(f"prompt {path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}")
    for name in sorted(TOOL_SPECS):
        out.append(f"tool {name} {json.dumps(TOOL_SPECS[name].schema(), sort_keys=True)}")
    for key in sorted(TOOL_ERRORS):
        out.append(f"tool_error {key} {TOOL_ERRORS[key]}")
    for graph in sorted(ALLOWLISTS):
        out.append(f"allowlist {graph} {sorted(ALLOWLISTS[graph])}")
    return out


@lru_cache(maxsize=1)
def bundle_sha256() -> str:
    return hashlib.sha256("\n".join(parts()).encode("utf-8")).hexdigest()
