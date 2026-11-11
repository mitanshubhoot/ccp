"""Test-only stand-ins for a task item run's model calls (A-151): a gatherer that searches
once per item question, and a reader that answers from the fenced chunks it was shown.

    a question about multi-factor authentication   yes, quoting the policy's sentence
    "fabricate" in the question                     yes, with a quote that is in no chunk
    "explode" in the question                       the call raises (a failed run)
    "ghost" in the question                         also a reading for an item not in the run
    "repairable" in the question                    a made-up quote first, the policy's real
                                                    sentence on the correction turn (A-152)
    "repeat" in the question                        the gatherer makes the same search twice
    a stored result in the newest message           the gatherer reads it by handle (A-153)
    a person's answer in the brief                  yes, citing the answer alone (A-154)
    anything else                                   insufficient_evidence, naming what is missing
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from ccp.evaluators.extraction.base import Cited
from ccp.evaluators.extraction.task_item import ItemFinding, ItemReadings, KeyedReading
from ccp.llm.client import StubLLMClient, SystemBlock, ToolCall
from ccp.security import prompt_guard
from tests.support.stub_extractors import blocks

_ITEM = re.compile(r"^Item (?P<id>\S+): (?P<question>.+)$", re.MULTILINE)
_IDS = re.compile(r"Return one reading for each of these item ids: (?P<ids>.+)$", re.MULTILINE)


def items_in(text: str) -> dict[str, str]:
    return {m["id"]: m["question"].strip() for m in _ITEM.finditer(text)}


def stored_handle(message: dict[str, Any]) -> str | None:
    """The handle of a stored result's preview in a tool_result message (A-153)."""
    for block in message.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        try:
            payload = json.loads(prompt_guard.unfence_tool_result(str(block.get("content"))))
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("stored"):
            return str(payload["handle"])
    return None


def gather(
    system: list[SystemBlock], messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> tuple[str, list[ToolCall]]:
    goal = str(messages[0].get("content", ""))
    questions = items_in(goal)
    handle = stored_handle(messages[-1]) if len(messages) == 3 else None
    if handle is not None:  # a stored result: read more of it by its handle
        return "", [ToolCall("call-more", "read_result", {"handle": handle, "offset": 0})]
    again = len(messages) == 3 and any("repeat" in q.lower() for q in questions.values())
    if len(messages) > 1 and not again:
        return "Found what the items need.", []
    return "", [
        ToolCall(
            f"call-{len(messages)}-{n}",
            "search_evidence",
            {"query": question[:200], "evidence_type": "policy_document"},
        )
        for n, question in enumerate(questions.values(), 1)
    ]


def _reading(item_id: str, question: str, user: str) -> KeyedReading:
    lowered = question.lower()
    if "explode" in lowered:
        raise RuntimeError("the model call failed")
    if "<<PERSON-ANSWER>>" in user:  # a reader that takes a person's word for it
        for block in blocks(user):
            if block.evidence_type == "human_answer":
                return KeyedReading(
                    item_id=item_id,
                    answer="yes",
                    reason="a person says so",
                    findings=[
                        ItemFinding(
                            statement="the person's answer",
                            citation=Cited(
                                chunk_id=block.chunk_id,
                                quote=block.body.strip().splitlines()[0][:200],
                            ),
                        )
                    ],
                )
    if "repairable" in lowered and "failed these code checks" not in user:
        lowered = lowered.replace("multi-factor", "fabricate")  # the first answer makes one up
    for block in blocks(user):
        for line in block.body.splitlines():
            if "multi-factor" in lowered and "Multi-factor authentication is required" in line:
                return KeyedReading(
                    item_id=item_id,
                    answer="yes",
                    reason="the policy requires MFA for every human identity",
                    findings=[
                        ItemFinding(
                            statement="MFA is required for human identities",
                            citation=Cited(chunk_id=block.chunk_id, quote=line.strip()),
                        )
                    ],
                )
    if "fabricate" in lowered:
        first = blocks(user)[0].chunk_id if blocks(user) else "none"
        return KeyedReading(
            item_id=item_id,
            answer="yes",
            reason="a confident answer",
            findings=[
                ItemFinding(
                    statement="made up",
                    citation=Cited(chunk_id=first, quote="Backups are encrypted with AES-256."),
                )
            ],
        )
    return KeyedReading(
        item_id=item_id,
        answer="insufficient_evidence",
        reason="no evidence speaks to this",
        missing=["a backup encryption standard"],
    )


def answer(schema: type[Any], system: list[SystemBlock], user: str) -> BaseModel:
    match = _IDS.search(user)
    ids = [i.strip() for i in match["ids"].split(",")] if match else []
    questions = items_in(user)
    readings = [_reading(i, questions.get(i, ""), user) for i in ids]
    if any("ghost" in q.lower() for q in questions.values()):
        readings.append(KeyedReading(item_id="q:ghost:read", answer="yes", reason="not asked"))
    return ItemReadings(readings=readings)


class TaskItemStub(StubLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.handler = answer
        self.chat_handler = gather
