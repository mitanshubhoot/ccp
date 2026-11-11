"""Settings: environment variables and the tenants file.

This is the only module that reads the environment. Everything else receives a
``Settings`` instance. ``DATABASE_ADMIN_URL`` is held here as a value but is read only
by the migration runner (``ccp.storage.db``) and the test database factory; a unit test
in ``tests/unit/test_layering.py`` enforces that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


def _zone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {value!r}; use an IANA name") from None
    return value


class ScheduleSpec(BaseModel):
    """One scheduled job of a tenant (A-108). `every` is `every N minutes`, `every N hours`
    or `daily HH:MM` on the wall clock of `time_zone` (default: the tenant's)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    job: Literal["clock", "unpark", "full_run", "check_connections", "sync", "replay_sentinel"]
    every: str = Field(
        pattern=r"^(every [1-9][0-9]* (minutes|hours)|daily ([01][0-9]|2[0-3]):[0-5][0-9])$"
    )
    time_zone: str | None = None

    _check_zone = field_validator("time_zone")(_zone)


DEFAULT_SCHEDULES: tuple[ScheduleSpec, ...] = (
    ScheduleSpec(id="clock", job="clock", every="every 15 minutes"),
    # the model budget's day is a UTC day, so parked work is released just after it turns
    ScheduleSpec(id="unpark", job="unpark", every="daily 00:05", time_zone="UTC"),
    # every connection probed, then pulled, before the nightly full run (A-121)
    ScheduleSpec(id="check-connections", job="check_connections", every="daily 01:00"),
    ScheduleSpec(id="sync", job="sync", every="daily 01:30"),
    ScheduleSpec(id="full-run", job="full_run", every="daily 02:00"),
    # a sample of tier-1 verdicts re-derived from what each read (A-125)
    ScheduleSpec(id="replay-sentinel", job="replay_sentinel", every="daily 03:30"),
)


class ConnectionSpec(BaseModel):
    """One connection of a tenant (A-121): a connector reading one provider account, from a
    fixture or live. `account` defaults to the tenant's GitHub organization for GitHub and to
    the tenant itself otherwise; `credential` names a purpose in `ccp.credentials`, never a
    secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: Literal["github", "hris", "manual"]
    source: Literal["fixture", "live"] = "fixture"
    account: str | None = Field(default=None, min_length=1)
    credential: Literal["github:read"] | None = None


DEFAULT_CONNECTIONS: tuple[ConnectionSpec, ...] = (
    ConnectionSpec(connector="github"),
    ConnectionSpec(connector="hris"),
    ConnectionSpec(connector="manual"),
)


class TenantSettings(BaseModel):
    """Per-tenant configuration from ``config/tenants.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str
    github_org: str
    org_admin_limit: int = Field(ge=0)
    critical_asset_tags: tuple[str, ...]
    termination_revocation_sla_hours: int = Field(gt=0)
    daily_budget_usd: float = Field(default=5.0, ge=0)
    max_concurrency: int = Field(default=4, ge=1)
    # What a reviewer's decisions may be used for (A-100): none, eval, or training. A tenant
    # admin opts in; each label keeps the scope in force when it was recorded.
    label_consent: Literal["none", "eval", "training"] = "none"
    # The tenant's wall clock for its schedules (A-108), an IANA name.
    time_zone: str = "UTC"
    schedules: tuple[ScheduleSpec, ...] = DEFAULT_SCHEDULES
    # The provider accounts it reads (A-121); one per connector and source.
    connections: tuple[ConnectionSpec, ...] = DEFAULT_CONNECTIONS

    @field_validator("time_zone")
    @classmethod
    def _real_zone(cls, value: str) -> str:
        checked = _zone(value)
        assert checked is not None
        return checked

    @field_validator("schedules")
    @classmethod
    def _unique_ids(cls, value: tuple[ScheduleSpec, ...]) -> tuple[ScheduleSpec, ...]:
        ids = [s.id for s in value]
        if len(ids) != len(set(ids)):
            raise ValueError(f"schedule ids repeat: {sorted(ids)}")
        return value

    @field_validator("connections")
    @classmethod
    def _one_per_source(cls, value: tuple[ConnectionSpec, ...]) -> tuple[ConnectionSpec, ...]:
        keys = [(c.connector, c.source) for c in value]
        if len(keys) != len(set(keys)):
            raise ValueError(f"more than one connection for a connector and source: {keys}")
        return value


class TenantsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenants: dict[str, TenantSettings]


class Settings(BaseSettings):
    """All runtime configuration. Field names map to environment variables case-insensitively."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = "postgresql://ccp_app:ccp@localhost:5432/ccp"
    database_admin_url: str = "postgresql://postgres:postgres@localhost:5432/ccp"
    # Agent tools read through their own read-only role (A-115). Unset: the application URL
    # with the user `ccp_agent` (same host, database and password).
    database_agent_url: str | None = None
    ccp_data_dir: Path = Path("data")
    ccp_tenants_file: Path = REPO_ROOT / "config" / "tenants.yaml"
    ccp_fixtures_dir: Path = REPO_ROOT / "fixtures"
    ccp_tenant: str | None = None

    # Queue (spec.md Layer 2, A-48, A-105): a parallel-topic claim is a lease of this many
    # seconds, renewed by the holder's heartbeat every third of it, so a live run of any
    # length keeps its records and a crashed worker's records are re-claimed one lease later.
    ccp_claim_visibility_seconds: float = Field(default=60, gt=0)
    ccp_max_attempts: int = Field(default=5, ge=1)
    # The first retry waits this long, doubling each try up to 15 minutes, with jitter (A-97).
    ccp_retry_base_seconds: float = Field(default=2.0, ge=0)
    # A connection with no complete pull for this long is stale (A-121): a daily sync, and
    # two hours' grace for a slow one.
    ccp_sync_stale_hours: int = Field(default=26, gt=0)
    ccp_golden_dir: Path = REPO_ROOT / "evals" / "golden"
    ccp_eval_runs_dir: Path = REPO_ROOT / "evals" / "runs"
    ccp_rubrics_dir: Path = REPO_ROOT / "evals" / "rubrics"
    ccp_labeled_dir: Path = REPO_ROOT / "evals" / "labeled"
    ccp_adversarial_path: Path = REPO_ROOT / "evals" / "adversarial" / "cases.yaml"
    ccp_slos_path: Path = REPO_ROOT / "config" / "slos.yaml"
    # Release gates on confidence bounds for the AI tests (A-103).
    ccp_eval_gates_path: Path = REPO_ROOT / "config" / "eval_gates.yaml"
    # Runs per model-backed eval case; pass^k counts a case only when all k pass (A-103).
    ccp_eval_runs: int = Field(default=1, ge=1)

    # PDF extraction caps (spec.md A-54); a file over any cap is rejected, never parsed.
    ccp_pdf_max_bytes: int = Field(default=25_000_000, ge=1)
    ccp_pdf_max_pages: int = Field(default=500, ge=1)
    ccp_pdf_timeout_seconds: float = Field(default=60.0, gt=0)

    # Models: pinned snapshot ids only (ccp/llm/models.py refuses aliases, A-103).
    anthropic_api_key: SecretStr | None = None
    ccp_llm_provider: Literal["anthropic"] = "anthropic"
    ccp_tier2_model: str = "claude-haiku-5-5"  # thinking off (A-100)
    ccp_tier3_model: str = "claude-sonnet-5"
    # The judge is a different model from every generating tier, and never grades its own
    # output; it runs ccp_judge_runs times and passes a case only when the runs agree.
    ccp_judge_model: str = "claude-opus-5"
    ccp_judge_runs: int = Field(default=3, ge=1)
    # A judge from another model family, for fixture suites only (Decision 9, A-103): it
    # never sees a case derived from customer data. None keeps the Claude judge everywhere.
    ccp_fixture_judge_model: str | None = None
    ccp_investigator_model: str | None = None
    ccp_policy_model: str | None = None
    ccp_copilot_model: str | None = None

    # Embeddings
    ccp_embed_provider: Literal["voyage", "openai"] = "voyage"
    ccp_embed_model: str = "voyage-3-large"
    voyage_api_key: SecretStr | None = None
    # Set true only after opting out of training in the Voyage organization settings; Voyage
    # clients are refused until then (A-96).
    voyage_training_opt_out: bool = False
    openai_api_key: SecretStr | None = None

    # Retrieval feature flags
    retrieval_rerank: bool = True
    retrieval_contextual: bool = True

    # Per-run budgets, enforced inside LLMClient (Phase 7a)
    ccp_run_budget_usd: float = Field(default=0.50, ge=0)
    ccp_run_budget_tokens: int = Field(default=60_000, ge=0)
    ccp_run_budget_seconds: int = Field(default=120, ge=0)

    # Tracing
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Connectors and remediation
    github_token: SecretStr | None = None
    ccp_remediation_live: bool = False
    ccp_remediation_config: Path = REPO_ROOT / "config" / "remediation.yaml"
    ccp_copilot_config: Path = REPO_ROOT / "config" / "copilot.yaml"

    @property
    def investigator_model(self) -> str:
        return self.ccp_investigator_model or self.ccp_tier3_model

    @property
    def policy_model(self) -> str:
        return self.ccp_policy_model or self.ccp_tier3_model

    @property
    def copilot_model(self) -> str:
        return self.ccp_copilot_model or self.ccp_tier3_model

    @property
    def logs_dir(self) -> Path:
        return self.ccp_data_dir / "logs"

    @property
    def drafts_dir(self) -> Path:
        """Policy drafts (Layer 15); never read as evidence."""
        return self.ccp_data_dir / "drafts"

    @property
    def uploads_dir(self) -> Path:
        """Runtime uploads root; one subdirectory per tenant."""
        return self.ccp_data_dir / "uploads"

    @property
    def raw_dir(self) -> Path:
        """The immutable raw zone (spec.md Layer 2)."""
        return self.ccp_data_dir / "raw"

    def load_tenants(self) -> dict[str, TenantSettings]:
        raw = yaml.safe_load(self.ccp_tenants_file.read_text(encoding="utf-8"))
        return TenantsFile.model_validate(raw).tenants
