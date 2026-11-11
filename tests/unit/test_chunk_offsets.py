"""MT9 (A-145): every chunk carries [start, end) offsets into its source text, and the
re-embedding gate refuses a candidate that measures worse than the serving generation.

The defects: a chunk could not be read back from the source it came from (the chunker
rejoins whitespace, so its text is not a substring), and a model change overwrote the
serving vectors with nothing measured first.
"""

from __future__ import annotations

from ccp.retrieval import reembed
from ccp.retrieval.chunker import MAX_CHARS, chunk_text, offsets
from ccp.retrieval.metrics import MetricsReport, QueryOutcome

SOURCE = (
    "  # Access Review Policy\n\n"
    "1. Purpose.   Reviews happen every quarter.\n"
    "   Owners sign off within 5 business days.\n\n\n"
    "| system | owner |\n|---|---|\n| GitHub | platform |\n\n"
    + ("A very long sentence without a stop " * 60)
    + "\n\nClosing paragraph."
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_offsets_read_each_chunk_back_from_its_source() -> None:
    windows = chunk_text(SOURCE)
    assert len(windows) > 2 and any(len(w) == MAX_CHARS for w in windows)  # one hard split
    spans = offsets(SOURCE, windows)
    assert all(span is not None for span in spans)
    previous_end = 0
    for window, span in zip(windows, spans, strict=True):
        assert span is not None
        start, end = span
        assert start >= previous_end and not SOURCE[start].isspace()
        assert _squash(SOURCE[start:end]).replace(" ", "") == window.replace(" ", "").replace(
            "\n", ""
        )
        previous_end = end


def test_offsets_never_guess() -> None:
    assert offsets("alpha beta", ["alpha", "gamma"]) == [(0, 5), None]
    assert offsets("alpha", ["alpha", "beta"]) == [(0, 5), None]
    assert offsets("", [""]) == [None]


def _report(recall: float, rr: float, queries: int = 3) -> MetricsReport:
    return MetricsReport(
        embed_model="m",
        rerank_model=None,
        include_distractors=False,
        iterative_scan=True,
        queries=[
            QueryOutcome(
                id=f"q{i}",
                recall_at_5=recall,
                reciprocal_rank=rr,
                ndcg_at_10=recall,
                relevant=1,
                retrieved=10,
                top_score=None,
            )
            for i in range(queries)
        ],
    )


def test_the_gate_refuses_a_worse_or_unmeasured_candidate() -> None:
    serving = _report(0.9, 0.8)
    assert reembed.gate(serving, _report(0.89, 0.79)).passed  # within the tolerance
    worse = reembed.gate(serving, _report(0.80, 0.80))
    assert not worse.passed and worse.reasons[0].startswith("recall_at_5 0.800 < serving 0.900")
    unmeasured = reembed.gate(serving, _report(1.0, 1.0, queries=0))
    assert not unmeasured.passed and "no retrieval golden queries" in unmeasured.reasons[0]
    assert reembed.gate(None, _report(0.5, 0.5)).passed  # nothing serving to lose against
    assert worse.as_json()["serving"]["recall_at_5"] == 0.9
