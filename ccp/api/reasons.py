"""Reason codes and the one action that clears each (spec.md A-135, report 6.10).

A status other than pass or fail is not an error: it says why CCP could not decide and what
would let it. Code writes these, from the codes the evaluators and the rollup already emit;
no model writes a reason. An unknown code still shows, with a generic next action, so a new
code is never hidden.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reason:
    code: str
    says: str
    next_action: str


# the rollup's pending reasons (A-124)
PENDING: dict[str, tuple[str, str]] = {
    "no_verdicts": ("not evaluated yet", "wait for the next scheduled run, or run one now"),
    "no_inventory": (
        "the full list of subjects is not known",
        "connect the source that lists them",
    ),
    "inventory_incomplete": (
        "the last pull did not see the whole list",
        "check the connection's health, then sync it again",
    ),
    "inventory_stale": ("the list of subjects is out of date", "sync the connection again"),
    "parked": (
        "waiting for an AI evaluation",
        "set the model key, or wait for the daily budget to reset",
    ),
    "unassessed": ("a subject has no evaluation yet", "wait for the next run, or run one now"),
    "stale": (
        "the evaluation is older than its revalidation window",
        "run the evaluation again",
    ),
}

# prefixes of the guardrail events a needs_review verdict carries
EVENTS: tuple[tuple[str, str, str], ...] = (
    ("incomplete_source:", "a source was not seen whole", "check the connection, then sync it"),
    ("no_evidence", "no evidence was found for the period", "upload the document for the period"),
    ("stale_evidence", "the evidence is out of date", "upload a current version"),
    (
        "injection_",
        "the evidence holds instructions aimed at a model",
        "review the document, and replace it if it was tampered with",
    ),
    ("citation_", "a quote could not be checked against the evidence", "review the citations"),
    ("interrupted", "waiting for a person to decide", "decide it in the inbox"),
    ("no_decision", "no reviewer decided it", "decide it in the inbox"),
    ("identity", "two records may be one person", "resolve the identity review"),
    ("provider", "the model provider was unavailable", "nothing: it runs again when it is back"),
    (
        "no_",
        "an observation the rule needs is missing",
        "check the connection, then sync it again",
    ),
)

GENERIC = ("CCP could not decide this alone", "open the verdict and review it")


def for_pending(code: str) -> Reason:
    says, action = PENDING.get(code, GENERIC)
    return Reason(code, says, action)


def for_events(events: list[str] | tuple[str, ...]) -> list[Reason]:
    """One reason per event a verdict carries, in order; unknown events keep their code."""
    out: list[Reason] = []
    for event in events:
        for prefix, says, action in EVENTS:
            if event.startswith(prefix):
                out.append(Reason(event, says, action))
                break
        else:
            if event.startswith(("forced_", "low_")):
                continue  # confidence caps: the factors show beside the verdict instead
            out.append(Reason(event, *GENERIC))
    return out
