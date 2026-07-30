"""Settings (env) + declarative collection registry (YAML, validated at startup).

Tenant onboarding = a config change, not a code change: collections, their
data classes and embedding versions live in config/collections.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .domain import CollectionCfg

# Whitelist for the Docker-secrets *_FILE convention (A5): only these fields
# may be filled from NAME_FILE paths — nothing else becomes file-loadable by
# accident.
SECRET_FILE_FIELDS: tuple[str, ...] = (
    "openai_api_key",
    "anthropic_api_key",
    "azure_openai_api_key",
    "api_key_anna",
    "api_key_ben",
    "database_url",
    "redis_url",
    "oidc_client_secret",
)


class SecretFileSource(PydanticBaseSettingsSource):
    """Docker-secrets pattern: NAME_FILE=/run/secrets/name fills NAME, but
    ONLY when NAME itself is unset — a directly set variable (env/.env)
    always wins; this source runs after them and merely fills the gaps.
    Fail-closed: a configured but missing/unreadable file aborts startup
    (same posture as validate_config_values), never a silent ignore."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Unused — __call__ assembles the whole value dict at once.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name in SECRET_FILE_FIELDS:
            path = os.environ.get(f"{name.upper()}_FILE")
            if not path:
                continue
            try:
                values[name] = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"{name.upper()}_FILE is set but not readable: {path}") from exc
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence (first wins): init/env/.env beat *_FILE — the file source
        # only fills what would otherwise stay at its default.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            SecretFileSource(settings_cls),
            file_secret_settings,
        )

    # infra
    database_url: str = "postgresql://rag_app:rag_app_pw@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"

    # deployment (A1) — orthogonal to rag_profile (E2 decision): the profile
    # routes providers, the mode hardens operations; offline+production must be
    # possible. production runs the fail-closed startup gate in readiness.py.
    deployment_mode: str = "demo"  # demo | production
    # static-key = demo auth (two API keys); oidc = OIDC/BFF (A6). Selecting
    # oidc without an issuer is a hard startup error, never a silent fallback.
    auth_backend: str = "static-key"  # static-key | oidc
    # OIDC (A6): BFF pattern — tokens never reach the browser; the browser only
    # holds an opaque HttpOnly session cookie. The issuer URL is the PUBLIC
    # (browser-facing) URL and is exactly what the iss claim must equal; the
    # internal URL is the container backchannel base for discovery/token/JWKS
    # fetches (empty = use the issuer URL). iss validation is NEVER relaxed.
    oidc_issuer_url: str = ""  # public issuer = iss claim (browser-facing)
    oidc_internal_issuer_url: str = ""  # backchannel base for containers; empty = issuer
    oidc_client_id: str = "rag-assistant"
    oidc_client_secret: str = ""  # confidential client; *_FILE-loadable (whitelist above)
    oidc_audience: str = ""  # empty = client_id
    oidc_claim_department: str = "department"
    # DEMO bridge ONLY (R10 forbids it in production): dex staticPasswords
    # cannot carry custom claims, so — only when the department claim is
    # missing AND this map is configured — email→department comes from this
    # JSON object. The email is read for the lookup, never stored.
    oidc_demo_department_map: str = ""  # JSON {email: department}
    auth_session_ttl_s: int = 28800  # 8 h — deletion-concept deadline
    session_cookie_secure: bool = False  # demo/localhost; operators set true behind TLS
    public_base_url: str = "http://localhost:8000"  # redirect_uri base (deterministic)
    # Rate limiting (A3, per identity — the system runs behind proxies, so
    # per-IP would be meaningless). Readiness rule R8 requires it in
    # production. burst = bucket capacity; refill rate = per_min / 60.
    # Defaults are demo-sized, explicitly NOT measured production values.
    rate_limit_enabled: bool = True
    rate_limit_query_per_min: int = 30
    rate_limit_query_burst: int = 10
    rate_limit_ingest_per_min: int = 10
    rate_limit_ingest_burst: int = 5
    # Server-side request size caps (A3): oversized payloads are rejected with
    # 413 before any model or worker is touched.
    max_question_chars: int = 2000
    max_ingest_bytes: int = 1_000_000
    # Audit trail (A4): access metadata only, append-only by grants (E3).
    # audit_fail_closed makes the confidential /query access event a hard
    # precondition of the stream (no token before a persisted event). R7
    # requires BOTH flags in production — enforced by the readiness GATE,
    # never by a runtime deployment_mode branch (E2). The SQL purge function
    # enforces a 30-day floor below audit_retention_days.
    audit_enabled: bool = True
    audit_fail_closed: bool = False
    audit_retention_days: int = 90
    # Structured logs (A5): true switches the root logger to a stdlib-JSON
    # formatter (obs.setup_logging); false keeps today's format byte-identical.
    log_json: bool = False

    # behaviour
    rag_profile: str = "default"  # default | offline
    run_mode: str = "dev"  # dev (native/MPS SLOs) | container (CPU SLOs)
    embeddings_backend: str = "local"  # local | fake
    reranker_backend: str = "local"  # local | fake | off
    # Pin the HF revision (commit hash or tag) of the local models instead of
    # tracking the moving default branch — supply-chain hygiene. Empty = latest.
    embedding_model_revision: str = ""
    reranker_model_revision: str = ""

    # OpenAI (primary cloud) — Azure is the SAME adapter via these flags
    openai_api_key: str = ""
    openai_model_mini: str = "gpt-5.6-luna"
    openai_model_strong: str = "gpt-5.6-terra"
    # Optional OpenAI-compatible endpoint override (sovereign EU service, vLLM).
    # Empty = api.openai.com. Azure settings take precedence when filled.
    openai_base_url: str = ""
    # Endpoint-specific request extras (A2): JSON object merged into every
    # chat request body, e.g. {"chat_template_kwargs": {"enable_thinking":
    # false}} for vLLM-class servers. Empty = none; parse errors abort startup
    # (fail-closed, see readiness.validate_config_values).
    openai_extra_body: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = ""
    azure_openai_deployment_mini: str = ""
    azure_openai_deployment_strong: str = ""

    # Eval judge (policy-gated, see policy.judge_provider_kinds — E1 decision).
    # Cloud judge for public/internal: Anthropic via its OpenAI-compatible /v1
    # endpoint (commercial API terms: no training, ~30-day retention). Judge must
    # differ from every generator family (OpenAI cloud, Qwen local) — Anthropic does.
    anthropic_api_key: str = ""
    judge_cloud_model: str = "claude-sonnet-5"
    judge_cloud_base_url: str = "https://api.anthropic.com/v1/"
    # Local judge for confidential (and offline profile): non-Qwen family, batch
    # use. Default per E7 decision, pending the calibration gate; override via env.
    ollama_judge_model: str = "gemma4:e4b"

    # Ollama (local generation)
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    # Non-streaming read timeout. Generous because a cold qwen3 on CPU must
    # load into RAM (~GBs) AND generate before the first byte — 120s times out
    # on the confidential path after an idle unload. Tunable via .env, no rebuild.
    ollama_timeout_s: float = 600.0
    # How long Ollama keeps the model resident after a call (Ollama duration
    # string, e.g. "30m", or "-1" to pin). Empty = Ollama's ~5min default.
    # Set OLLAMA_KEEP_ALIVE=30m for eval runs so qwen3 stays warm across the
    # long retrieval phase instead of cold-loading on every confidential answer.
    ollama_keep_alive: str = ""

    # demo users (API key → user mapping; production would use SSO/OIDC)
    api_key_anna: str = "demo-anna-it"
    api_key_ben: str = "demo-ben-hr"

    # retrieval knobs (documented quality-vs-latency dials, part of the eval matrix)
    retrieve_top_k: int = 30
    context_top_n: int = 5
    context_token_budget: int = 4000
    rrf_k: int = 60

    # agentic guards
    max_loop_iterations: int = 2
    loop_token_budget: int = 6000

    # cache / sessions
    answer_cache_ttl_s: int = 3600
    session_ttl_s: int = 86400
    # Retention for feedback rows (deletion concept): a daily worker cron purges
    # older rows. Demo default — operators set this per their retention schedule.
    feedback_retention_days: int = 180

    config_dir: Path = Path("config")


class Registry(BaseModel):
    """Validated view of config/collections.yaml."""

    default_tenant: str
    default_collection: str
    collections: dict[str, CollectionCfg]

    def get(self, name: str) -> CollectionCfg | None:
        return self.collections.get(name)


def load_registry(config_dir: Path) -> Registry:
    path = config_dir / "collections.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cols = {c["name"]: CollectionCfg(**{**c, "tenant": raw["tenant"]}) for c in raw["collections"]}
    default_collection = raw["default_collection"]
    if default_collection not in cols:
        raise ValueError(f"default_collection '{default_collection}' not defined in {path}")
    return Registry(
        default_tenant=raw["tenant"],
        default_collection=default_collection,
        collections=cols,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_registry() -> Registry:
    return load_registry(get_settings().config_dir)
