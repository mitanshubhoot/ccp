"""Settings: environment variables and the tenants file.

This is the only module that reads the environment. Everything else receives a
``Settings`` instance. ``DATABASE_ADMIN_URL`` is held here as a value but is read only
by the migration runner (``ccp.storage.db``) and the test database factory; a unit test
in ``tests/unit/test_layering.py`` enforces that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


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


class TenantsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenants: dict[str, TenantSettings]


class Settings(BaseSettings):
    """All runtime configuration. Field names map to environment variables case-insensitively."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = "postgresql://ccp_app:ccp@localhost:5432/ccp"
    database_admin_url: str = "postgresql://postgres:postgres@localhost:5432/ccp"
    ccp_data_dir: Path = Path("data")
    ccp_tenants_file: Path = REPO_ROOT / "config" / "tenants.yaml"
    ccp_fixtures_dir: Path = REPO_ROOT / "fixtures"
    ccp_tenant: str | None = None

    # Queue (spec.md Layer 2, A-48): parallel-topic claims expire after this many seconds,
    # which must exceed the run wall-clock budget so a live run is never re-claimed.
    ccp_claim_visibility_seconds: int = Field(default=300, ge=1)
    ccp_max_attempts: int = Field(default=5, ge=1)
    # The first retry waits this long, doubling each try up to 15 minutes, with jitter (A-97).
    ccp_retry_base_seconds: float = Field(default=2.0, ge=0)
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
