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

from contracts.approval_token import MIN_KEY_BYTES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM (diagnoser) ---
    openai_api_key: SecretStr | None = None
    llm_model: str = ""
    # OpenAI-compatible endpoint for the diagnoser's LLM (diagnoser/openai_chat.py). Empty = the
    # OpenAI API. Set it to use another OpenAI-compatible server (e.g. a local Ollama at
    # http://localhost:11434/v1) with no code change (DECISIONS: LLM row).
    llm_base_url: str = ""
    # Per-call timeout and the SDK's own retry count for transient errors (429, 5xx, network).
    # The reasoner itself never retries a provider error (it applies its safe default instead).
    llm_timeout_s: float = Field(default=60.0, ge=5.0, le=600.0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_model: str = ""

    # --- GCP ---
    gcp_project: str = ""
    firestore_database: str = "(default)"
    executor_url: str = "http://localhost:8080"
    # What the diagnoser's check_health curls (via the executor) after a fix. It runs ON the VM,
    # so this is the proxy as seen from there. Must be one of the executor allowlist's exact
    # curl URLs (src/security/allowlist.yaml), or every health check comes back REJECTED.
    health_url: str = "http://127.0.0.1:8088/health"
    # Bearer token callers of POST /incidents must send (the monitor, the eval harness).
    # The diagnoser service has to be publicly reachable for Slack's webhook, so incident intake
    # needs its own check. Unset = intake refuses everything (fails closed).
    incident_api_token: SecretStr | None = None

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
    # Audience the diagnoser's Google ID token must carry. Empty = use executor_url (the usual
    # choice: Cloud Run mints ID tokens for the URL it is calling).
    executor_audience: str = ""
    # sqlite file for the remediation idempotency store. Must survive executor restarts.
    executor_state_path: str = "var/executor-state.sqlite3"

    # --- tracing ---
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "incident-responder"

    def approval_key_bytes(self) -> bytes:
        """The approval-token HMAC key as bytes. The ONLY way either service should get it.

        WHY this exists (raised by Sai on PR #2): the env var is text, but mint()/verify() need
        bytes. If the Slack handler did `bytes.fromhex(value)` and the executor did
        `value.encode()`, they would hold different keys and every approval would fail
        verification, or someone would "fix" it by weakening the check. One decoder, used by both
        sides, makes that impossible.

        Format: hex, at least 32 bytes after decoding (64 hex characters). Generate one with
            uv run python -c "import secrets; print(secrets.token_hex(32))"
        Surrounding whitespace is ignored (a trailing newline from Secret Manager or a copy-paste
        must not change the key).

        Raises ValueError if the key is unset, not hex, or too short. The message never includes
        the value itself.
        """
        if (
            self.approval_signing_key is None
            or not self.approval_signing_key.get_secret_value().strip()
        ):
            raise ValueError("APPROVAL_SIGNING_KEY is not set")
        try:
            key = bytes.fromhex(self.approval_signing_key.get_secret_value().strip())
        except ValueError:
            # `from None`: the original error message quotes the bad character position; drop it
            # so nothing about the secret reaches logs.
            raise ValueError(
                "APPROVAL_SIGNING_KEY must be hex; generate one with "
                '`uv run python -c "import secrets; print(secrets.token_hex(32))"`'
            ) from None
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(
                f"APPROVAL_SIGNING_KEY decodes to {len(key)} bytes; at least {MIN_KEY_BYTES} are "
                f"required ({MIN_KEY_BYTES * 2} hex characters)"
            )
        return key

    @property
    def approver_ids(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.approver_allowlist.split(",") if x.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so every module sees the same values. Tests can call get_settings.cache_clear()."""
    return Settings()
