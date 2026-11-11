"""Method cards and published quality figures (spec.md A-170).

A method card says how one verdict or one suggestion came to be, in facts code recorded:
who decided (a rule or a model), the rule version or the model, prompt and release bundle,
the catalogue version, the evidence read and its provenance, the checks applied, and, for
a model's verdict, the quality its workflow last published. Model prose is never the card.

A published snapshot says, per AI workflow and release bundle, how good the workflow is:
the false-PASS rate with its upper bound and the count of FAIL-labelled cases it rests on,
pass^k with k, the bundle's models and prompt versions, the gold set's version, the date
of the last re-certification and the method (config/eval_method.yaml). A bound resting on
fewer FAIL-labelled cases than the method's minimum is withheld, with the count shown, so
no figure is published on a gold set too small to support it. `ccp evals publish` writes
one snapshot per workflow under evals/published/<quarter>/.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ccp.config import REPO_ROOT, Settings
from ccp.evals import sizing
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.adequacy import clopper_pearson_upper
from ccp.llm import release
from ccp.llm.prompts import prompt_files
from ccp.storage.scoped import ScopedDB

METHOD_FILE = REPO_ROOT / "config" / "eval_method.yaml"
PUBLISHED_DIR = REPO_ROOT / "evals" / "published"
WORKFLOW_OF_TIER = {2: "tier2_verdict", 3: "tier3_verdict"}


def method() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(METHOD_FILE.read_text(encoding="utf-8")) or {}
    return raw


def quarter(moment: datetime) -> str:
    return f"{moment.year}-Q{(moment.month - 1) // 3 + 1}"


@dataclass(frozen=True)
class Snapshot:
    workflow: str
    bundle_id: str
    quarter: str
    cases: int
    fail_labelled: int
    false_passes: int
    false_pass_rate: float | None
    false_pass_upper: float | None  # withheld (None) below the method's minimum
    withheld: str | None
    pass_k: float | None
    k: int
    models: dict[str, str]
    prompt_versions: list[str]
    dataset_sha256: str | None
    last_recertified: str | None
    method: dict[str, Any]
    published_at: str


def _last_recertified(runs_dir: Path) -> str | None:
    found = sorted(runs_dir.glob("calibration-*.json"))
    if not found:
        return None
    stamp = found[-1].stem.split("-", 1)[1]
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC).date().isoformat()
    except ValueError:
        return stamp


def snapshots(
    report: dict[str, Any], settings: Settings, now: datetime | None = None
) -> list[Snapshot]:
    moment = now or datetime.now(UTC)
    rules = method()
    minimum = int(rules.get("min_fail_cases", 300))
    manifest = release.manifest(settings)
    bundle = release.bundle_id(settings)
    prompts = [path.stem for path in prompt_files()]
    out: list[Snapshot] = []
    for workflow, stats in sorted(((report.get("metrics") or {}).get("ai_tests") or {}).items()):
        n = int(stats.get("fail_labelled") or 0)
        false_pass = stats.get("false_pass") or {}
        rate = false_pass.get("estimate")
        failures = round(float(rate or 0) * n)
        if n < minimum:
            upper, withheld = None, f"{n} FAIL-labelled cases; at least {minimum} are needed"
        else:
            upper = round(
                sizing.upper_bound(n) if failures == 0 else clopper_pearson_upper(failures, n), 4
            )
            withheld = None
        pass_k = (stats.get("pass_k") or {}).get("estimate")
        out.append(
            Snapshot(
                workflow=workflow,
                bundle_id=bundle,
                quarter=quarter(moment),
                cases=int(stats.get("cases") or 0),
                fail_labelled=n,
                false_passes=failures,
                false_pass_rate=rate,
                false_pass_upper=upper,
                withheld=withheld,
                pass_k=pass_k,
                k=int(stats.get("runs_per_case") or 1),
                models=dict(manifest.get("models") or release.roles(settings)),
                prompt_versions=prompts,
                dataset_sha256=report.get("golden_sha256"),
                last_recertified=_last_recertified(settings.ccp_eval_runs_dir),
                method=rules,
                published_at=moment.isoformat(),
            )
        )
    return out


def publish(
    report_path: Path,
    settings: Settings,
    out_dir: Path = PUBLISHED_DIR,
    now: datetime | None = None,
) -> list[Path]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    written: list[Path] = []
    for snap in snapshots(report, settings, now):
        folder = out_dir / snap.quarter
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{snap.workflow}-{snap.bundle_id}.json"
        path.write_text(json.dumps(asdict(snap), indent=2), encoding="utf-8")
        written.append(path)
    return written


def latest_published(workflow: str, out_dir: Path = PUBLISHED_DIR) -> dict[str, Any] | None:
    found = sorted(
        out_dir.glob(f"*/{workflow}-*.json"), key=lambda p: (p.parent.name, p.stat().st_mtime)
    )
    return json.loads(found[-1].read_text(encoding="utf-8")) if found else None


# -- method cards ---------------------------------------------------------------------------


def verdict_card(
    db: ScopedDB, verdict_id: str, out_dir: Path = PUBLISHED_DIR
) -> dict[str, Any] | None:
    """How one verdict came to be, from what code recorded."""
    found = verdicts.load(db, verdict_id)
    if found is None:
        return None
    by_rule = found.tier == 1
    evidence = []
    for ref in found.evidence_refs:
        row = db.fetch_one(
            "SELECT source_connector FROM (SELECT entity_id, source_connector, observed_at FROM"
            " evidence UNION ALL SELECT entity_id, source_connector, observed_at FROM assets"
            " UNION ALL SELECT entity_id, source_connector, observed_at FROM access_grants"
            " UNION ALL SELECT entity_id, source_connector, observed_at FROM persons) e"
            " WHERE entity_id = %s ORDER BY observed_at DESC LIMIT 1",
            (ref,),
        )
        connector = str(row[0]) if row else "unknown"
        evidence.append(
            {
                "entity_id": ref,
                "connector": connector,
                "provenance": "customer_attested" if connector == "manual" else "system_observed",
            }
        )
    card: dict[str, Any] = {
        "verdict_id": found.verdict_id,
        "test_id": found.test_id,
        "subject_key": found.subject_key,
        "status": found.status.value,
        "decided_by": "rule" if by_rule else "model",
        "tier": found.tier,
        "rule_version": found.rule_version,
        "model": found.model,
        "prompt_version": found.prompt_version,
        "bundle_id": found.bundle_id,
        "catalogue_version": (found.manifest or {}).get("catalogue_version"),
        "evidence": evidence,
        "checks": {
            "citations": len(found.citations),
            "citations_verified": sum(1 for c in found.citations if c.verified),
            "guardrail_events": list(found.guardrail_events),
        },
        "confidence": {"value": found.confidence, "cap": found.confidence_cap},
        "quality": None,
    }
    if not by_rule:
        snap = latest_published(WORKFLOW_OF_TIER.get(found.tier, ""), out_dir)
        card["quality"] = (
            {
                k: snap[k]
                for k in (
                    "quarter",
                    "bundle_id",
                    "false_pass_upper",
                    "withheld",
                    "fail_labelled",
                    "pass_k",
                    "k",
                )
            }
            if snap
            else "no published figures for this workflow yet"
        )
    return card


def suggestion_card(db: ScopedDB, suggestion_id: str) -> dict[str, Any] | None:
    """How one fix suggestion came to be (A-159, A-160)."""
    row = db.fetch_one(
        "SELECT suggestion_id, source, fix, fix_version, template_sha256, checks, lookup_ids,"
        " status, verdict_id FROM fix_suggestion_status WHERE suggestion_id = %s",
        (suggestion_id,),
    )
    if row is None:
        return None
    load = json.loads
    return {
        "suggestion_id": row[0],
        "chosen_by": "code" if row[1] == "code" else "model, bounded to the catalogue",
        "rendered_by": "code, from a versioned template",
        "fix": row[2],
        "fix_version": row[3],
        "template_sha256": row[4],
        "checks": load(row[5]) if isinstance(row[5], str) else row[5],
        "lookups": load(row[6]) if isinstance(row[6], str) else row[6],
        "status": row[7],
        "for_verdict": row[8],
        "applied_by": "a person; CCP confirms only from a later read",
    }
