from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "gateway.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    local_api_token: str = ""
    internal_token: str = ""
    signing_secret: str = ""
    model_router_url: str = "http://127.0.0.1:8742"
    model_router_timeout_sec: float = 15.0
    orchestrator_url: str = "http://127.0.0.1:8743"
    orchestrator_timeout_sec: float = 300.0
    enable_whatsapp: bool = True
    sessions_db_path: Path = BACKEND_ROOT / "gateway" / "sessions.db"
    routing_audit_db_path: Path = BACKEND_ROOT / "gateway" / "routing_audit.db"
    haiku_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5"
    anthropic_version: str = "2023-06-01"
    haiku_max_tokens: int = 16000
    haiku_thinking_budget_tokens: int = 10000
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    direct_llm_timeout_sec: float = 90.0

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=_env_int("GATEWAY_PORT", 8080),
            local_api_token=(
                os.getenv("GATEWAY_LOCAL_API_TOKEN")
                or os.getenv("LOCAL_API_TOKEN")
                or ""
            ),
            internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", ""),
            signing_secret=os.getenv("GATEWAY_SIGNING_SECRET", "").strip(),
            model_router_url=os.getenv("MODEL_ROUTER_URL", "http://127.0.0.1:8742").rstrip("/"),
            model_router_timeout_sec=max(
                1.0,
                _env_float("MODEL_ROUTER_TIMEOUT_SEC", 15.0),
            ),
            orchestrator_url=os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8743").rstrip("/"),
            orchestrator_timeout_sec=max(
                10.0,
                _env_float("ORCHESTRATOR_TIMEOUT_SEC", 300.0),
            ),
            enable_whatsapp=_env_bool("WHATSAPP_ENABLED", True),
            sessions_db_path=Path(
                os.getenv("GATEWAY_SESSIONS_DB_PATH", str(BACKEND_ROOT / "gateway" / "sessions.db"))
            ).expanduser(),
            routing_audit_db_path=Path(
                os.getenv(
                    "GATEWAY_ROUTING_AUDIT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "routing_audit.db"),
                )
            ).expanduser(),
            haiku_api_key=(
                os.getenv("HAIKU_API_KEY")
                or os.getenv("ANTHROPIC_API_KEY")
                or os.getenv("GEMINI_API_KEY")
                or ""
            ).strip(),
            haiku_model=(
                os.getenv("HAIKU_MODEL")
                or os.getenv("GEMINI_MODEL")
                or "claude-haiku-4-5"
            ).strip(),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01").strip(),
            haiku_max_tokens=max(1024, _env_int("HAIKU_MAX_TOKENS", 16000)),
            haiku_thinking_budget_tokens=max(
                0,
                _env_int("HAIKU_THINKING_BUDGET_TOKENS", 10000),
            ),
            perplexity_api_key=os.getenv("PERPLEXITY_API_KEY", "").strip(),
            perplexity_model=os.getenv("PERPLEXITY_MODEL", "sonar").strip(),
            direct_llm_timeout_sec=max(
                5.0,
                _env_float("DIRECT_LLM_TIMEOUT_SEC", 90.0),
            ),
        )
