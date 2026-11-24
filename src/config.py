"""Typed configuration, loaded from environment variables (and `.env` in local dev).

WHAT: one Settings object with every variable from docs/INTERFACES.md §6.
WHY:  AGENTS.md rule 6: no hard-coded model names, hosts or limits. Loading through
      pydantic-settings means a typo like MAX_TOOL_CALLS=thirty fails at startup with a clear
      error, instead of becoming a string that breaks a comparison halfway through an incident.
HOW:  `from config import get_settings; s = get_settings(); s.max_tool_calls`

Secrets are `SecretStr`, so they print as '**********' if a Settings object is ever logged.
Every field has a safe default so tests and `import` work with no .env at all; services that
need a secret (e.g. the executor needs APPROVAL_SIGNING_KEY) check for it at startup themselves.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM (diagnoser) ---
    openai_api_key: SecretStr | None = None
    llm_model: str = ""
    embedding_model: str = ""

    # --- GCP ---
    gcp_project: str = ""
    firestore_database: str = "(default)"
    executor_url: str = "http://localhost:8080"

    # --- Slack approval ---
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel_id: str = ""
    # Comma-separated in the env var; split by `approver_ids`.
    approver_allowlist: str = ""

    # --- approval token (§4.3) ---
    approval_signing_key: SecretStr | None = None
    approval_ttl_s: int = Field(default=900, ge=60, le=3600)

    # --- budgets (§1). Bounded so a bad env value can't disable a cap. ---
    max_iterations: int = Field(default=12, ge=1, le=100)
    max_replans: int = Field(default=3, ge=0, le=20)
    max_tool_calls: int = Field(default=30, ge=1, le=500)
    max_tokens_per_incident: int = Field(default=150_000, ge=1_000, le=2_000_000)
    max_wall_clock_s: int = Field(default=600, ge=30, le=7200)

    # --- ablation switches (§5) ---
    memory_enabled: bool = True
    max_hypotheses: int = Field(default=3, ge=1, le=10)

    # --- executor ---
    allowed_diagnoser_sa: str = ""
    max_output_bytes: int = Field(default=16_384, ge=1_024, le=1_048_576)

    # --- tracing ---
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "incident-responder"

    @property
    def approver_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.approver_allowlist.split(",") if x.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so every module sees the same values. Tests can call get_settings.cache_clear()."""
    return Settings()
