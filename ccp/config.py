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
    ccp_tenant: str | None = None

    # Models
    anthropic_api_key: SecretStr | None = None
    ccp_llm_provider: Literal["anthropic"] = "anthropic"
    ccp_tier2_model: str = "claude-haiku-4-5"
    ccp_tier3_model: str = "claude-sonnet-5"
    ccp_judge_model: str = "claude-haiku-4-5"
    ccp_investigator_model: str | None = None
    ccp_policy_model: str | None = None
    ccp_copilot_model: str | None = None

    # Embeddings
    ccp_embed_provider: Literal["voyage", "openai"] = "voyage"
    ccp_embed_model: str = "voyage-3-large"
    voyage_api_key: SecretStr | None = None
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

    def load_tenants(self) -> dict[str, TenantSettings]:
        raw = yaml.safe_load(self.ccp_tenants_file.read_text(encoding="utf-8"))
        return TenantsFile.model_validate(raw).tenants
