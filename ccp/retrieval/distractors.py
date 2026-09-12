"""Generated distractor chunks (spec.md D25, Layer 11): prior-year reviews, other employees'
certificates, near-duplicate policy versions, unrelated IT documents. Deterministic per
(tenant, seed). They live in `chunks` with is_distractor = true, are never retrieved by an
evaluator, and give the retrieval metrics and the percentile calibration something to be
wrong about."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass

from ccp.retrieval import cache
from ccp.retrieval.chunker import CHUNKER_VERSION, chunk_text
from ccp.retrieval.embeddings import EmbeddingClient, vector_literal
from ccp.storage.scoped import ScopedDB

FIRST_NAMES = (
    "Alex", "Bea", "Carlos", "Dev", "Elena", "Farid", "Grace", "Hiro", "Isla", "Jonas",
    "Kai", "Lena", "Mateo", "Nour", "Omar", "Pia", "Quinn", "Rosa", "Sven", "Tara",
    "Uma", "Viktor", "Wren", "Ximena", "Yusuf", "Zoe",
)  # fmt: skip
LAST_NAMES = (
    "Adler", "Brennan", "Castillo", "Dubois", "Eriksen", "Fischer", "Gupta", "Haddad",
    "Ivanova", "Jansen", "Kowalski", "Lindqvist", "Moreau", "Nakamura", "Okoro", "Petrov",
    "Quintero", "Rahman", "Sato", "Tremblay", "Ueda", "Varga", "Weber", "Xu", "Yilmaz", "Zhang",
)  # fmt: skip
SYSTEMS = ("GitHub", "Okta", "AWS-Prod", "Payroll", "Datadog", "Jira", "Slack", "Zendesk", "GCP")
PROVIDERS = ("KnowBe4", "SANS Security Awareness", "Proofpoint", "Curricula")
RESULTS = ("no changes", "1 removal", "2 role reductions")
FINDINGS = (
    "No dormant accounts were found.",
    "One dormant account was removed.",
    "Two privileged roles were reduced.",
)
TOPICS = (
    (
        "Acceptable Use Policy",
        "Company devices are for business use. Personal use is permitted when it does not"
        " interfere with work or expose company data.",
    ),
    (
        "Backup Policy",
        "Production databases are backed up nightly and retained for 35 days. Restores are"
        " tested quarterly.",
    ),
    (
        "Data Retention Policy",
        "Customer records are retained for the life of the contract plus seven years, then"
        " deleted from every system.",
    ),
    (
        "Clean Desk Policy",
        "Printed material containing confidential data is locked away when the desk is unattended.",
    ),
    (
        "Remote Work Policy",
        "Remote workers connect through the company VPN and keep screens locked when away.",
    ),
)


@dataclass(frozen=True)
class Distractor:
    template: str
    evidence_type: str
    subject_entity_id: str | None
    title: str
    text: str


def _name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def _date(rng: random.Random, year: int) -> str:
    return f"{year}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def _review(rng: random.Random, i: int) -> Distractor:
    year = rng.choice((2023, 2024, 2025))
    quarter = rng.choice(("Q1", "Q2", "Q3", "Q4"))
    systems = rng.sample(SYSTEMS, k=rng.randint(2, 5))
    rows = "\n".join(
        f"| {s} | {_name(rng)} | {_date(rng, year)} | {rng.choice(RESULTS)} |" for s in systems
    )
    text = (
        f"# {quarter} {year} User Access Review\n\nThis review confirms that access to the "
        f"in-scope systems was limited to personnel with a current business need during "
        f"{quarter} {year}. Reviewer: {_name(rng)}.\n\n"
        f"## Scope\n\n| System | Owner | Reviewed on | Result |\n|---|---|---|---|\n{rows}\n\n"
        f"## Findings\n\n{rng.choice(FINDINGS)} The next review is due the following quarter."
    )
    return Distractor(
        "prior_year_review", "access_review_report", None, f"{quarter} {year} review", text
    )


def _certificate(rng: random.Random, i: int) -> Distractor:
    name = _name(rng)
    employee = f"DX{i:04d}"
    year = rng.choice((2024, 2025, 2026))
    status = rng.choice(("Completed", "Completed", "Completed", "In progress"))
    completed = status == "Completed"
    text = (
        f"# Security Awareness Training Certificate\n\nThis certifies that **{name}** (employee "
        f"{employee}) {'completed' if completed else 'is attending'} the Security Awareness "
        f"Training {year}, covering phishing, credential hygiene, data handling, and incident "
        f"reporting.\n\nStatus: {status}\n"
        f"Completion date: {_date(rng, year) if completed else 'n/a'}\n"
        f"Provider: {rng.choice(PROVIDERS)}\nScore: {rng.randint(70, 100)}%"
    )
    return Distractor(
        "other_employee_certificate", "training_record", f"hris:person:{employee}", name, text
    )


def _policy_variant(rng: random.Random, i: int) -> Distractor:
    days = rng.choice((2, 3, 7, 10, 14, 21))
    unit = rng.choice(("business days", "days", "working days", "hours"))
    plan = rng.choice((14, 30, 45, 60, 90))
    version = rng.randint(1, 9)
    text = (
        f"# Security Policy (draft v{version})\n\n## Reporting a vulnerability\n\nEmail the "
        f"security team with a description of the issue and steps to reproduce. Do not open a "
        f"public issue.\n\n## Response timeline\n\nWe acknowledge every report within {days} "
        f"{unit}. Confirmed vulnerabilities receive a remediation plan within {plan} days of "
        f"acknowledgement.\n\n## Safe harbour\n\nGood-faith research that follows this policy "
        f"will not be subject to legal action."
    )
    return Distractor(
        "near_duplicate_policy",
        "policy_document",
        f"github:asset:R_distractor{i:04d}",
        f"Security Policy draft v{version}",
        text,
    )


def _misc_policy(rng: random.Random, i: int) -> Distractor:
    title, body = rng.choice(TOPICS)
    text = (
        f"# {title} (v{rng.randint(1, 5)})\n\n## Purpose\n\n{body}\n\n## Scope\n\nThis policy "
        f"applies to all staff and contractors.\n\n## Review\n\nReviewed annually by the "
        f"security team; last reviewed {_date(rng, rng.choice((2024, 2025, 2026)))}."
    )
    return Distractor("unrelated_policy", "policy_document", None, title, text)


Maker = Callable[[random.Random, int], Distractor]
MAKERS: tuple[Maker, ...] = (_review, _certificate, _policy_variant, _misc_policy)
WEIGHTS = (0.33, 0.33, 0.2, 0.14)


def generate(tenant_id: str, count: int = 300, seed: int | None = None) -> list[Distractor]:
    if seed is None:
        seed = int(hashlib.sha256(tenant_id.encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)  # noqa: S311 - deterministic test data, not security material
    return [rng.choices(MAKERS, weights=WEIGHTS, k=1)[0](rng, i) for i in range(count)]


@dataclass(frozen=True)
class _Draft:
    chunk_id: str
    evidence_entity_id: str
    distractor: Distractor
    ordinal: int
    text: str


def _drafts(tenant_id: str, count: int, seed: int | None) -> list[_Draft]:
    out: list[_Draft] = []
    for i, d in enumerate(generate(tenant_id, count, seed)):
        evidence_id = f"distractor:{i:04d}:{d.template}"
        for ordinal, window in enumerate(chunk_text(d.text)):
            material = f"{tenant_id}|{evidence_id}|{CHUNKER_VERSION}|None|{ordinal}|{window}"
            chunk_id = hashlib.sha256(material.encode()).hexdigest()[:32]
            out.append(_Draft(chunk_id, evidence_id, d, ordinal, window))
    return out


def install(
    db: ScopedDB, embedder: EmbeddingClient, count: int = 300, seed: int | None = None
) -> int:
    """Chunk, embed (cached), and insert; returns the number of distractor chunks present."""
    drafts = _drafts(db.tenant.tenant_id, count, seed)
    existing = {
        str(r[0])
        for r in db.fetch_all(
            "SELECT chunk_id FROM chunks WHERE is_distractor AND embed_model = %s",
            (embedder.model_id,),
        )
    }
    pending = [d for d in drafts if d.chunk_id not in existing]
    keys = {d.chunk_id: cache.embedding_key(embedder.model_id, "document", d.text) for d in pending}
    cached = cache.get_embeddings(db, list(set(keys.values())))
    vectors: dict[str, list[float]] = {
        d.chunk_id: cached[keys[d.chunk_id]] for d in pending if keys[d.chunk_id] in cached
    }
    missing = [d for d in pending if keys[d.chunk_id] not in cached]
    if missing:
        embedded = embedder.embed([d.text for d in missing], "document")
        new_entries: dict[str, list[float]] = {}
        for draft, vector in zip(missing, embedded.vectors, strict=True):
            vectors[draft.chunk_id] = vector
            new_entries[keys[draft.chunk_id]] = vector
        cache.put_embeddings(db, embedder.model_id, new_entries)
    with db.transaction() as cur:
        for d in pending:
            cur.execute(
                "INSERT INTO chunks (chunk_id, tenant_id, evidence_entity_id, evidence_type,"
                " subject_entity_id, source_ref, page, ordinal, text, embedding, embed_model,"
                " chunker_version, text_sha256, is_distractor)"
                " VALUES (%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s::vector,%s,%s,%s,true)"
                " ON CONFLICT (chunk_id) DO NOTHING",
                (
                    d.chunk_id,
                    db.tenant.tenant_id,
                    d.evidence_entity_id,
                    d.distractor.evidence_type,
                    d.distractor.subject_entity_id,
                    f"distractor:{d.distractor.template}",
                    d.ordinal,
                    d.text,
                    vector_literal(vectors[d.chunk_id]),
                    embedder.model_id,
                    CHUNKER_VERSION,
                    hashlib.sha256(d.text.encode()).hexdigest(),
                ),
            )
    row = db.fetch_one("SELECT count(*) FROM chunks WHERE is_distractor")
    return int(row[0]) if row else 0


def clear(db: ScopedDB) -> int:
    return db.execute("DELETE FROM chunks WHERE is_distractor")
