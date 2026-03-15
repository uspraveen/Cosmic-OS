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


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return values or default


@dataclass(slots=True)
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    public_host: str = ""
    local_api_token: str = ""
    internal_token: str = ""
    signing_secret: str = ""
    model_router_url: str = "http://127.0.0.1:8742"
    model_router_timeout_sec: float = 15.0
    orchestrator_url: str = "http://127.0.0.1:8743"
    orchestrator_timeout_sec: float = 300.0
    redis_url: str = ""
    task_input_requests_stream: str = "user_input:requests"
    task_input_replies_stream: str = "user_input:replies"
    task_input_gateway_group: str = "gateway"
    enable_whatsapp: bool = True
    enable_telegram: bool = False
    sessions_db_path: Path = BACKEND_ROOT / "gateway" / "sessions.db"
    routing_audit_db_path: Path = BACKEND_ROOT / "gateway" / "routing_audit.db"
    memory_write_audit_db_path: Path = BACKEND_ROOT / "gateway" / "memory_write_audit.db"
    artifacts_db_path: Path = BACKEND_ROOT / "gateway" / "artifacts.db"
    delivery_queue_db_path: Path = BACKEND_ROOT / "gateway" / "delivery_queue.db"
    scheduler_db_path: Path = BACKEND_ROOT / "gateway" / "scheduler.db"
    session_transcript_dir: Path = BACKEND_ROOT / "logs" / "sessions"
    session_reset_hour: int = 4
    user_timezone_fallback: str = "America/Chicago"
    scheduler_poll_interval_sec: float = 30.0
    delivery_retry_base_sec: float = 1.0
    delivery_retry_max_sec: float = 120.0
    delivery_max_attempts: int = 12
    haiku_api_key: str = ""
    haiku_model: str = "claude-haiku-4-5"
    anthropic_version: str = "2023-06-01"
    haiku_max_tokens: int = 16000
    haiku_thinking_budget_tokens: int = 10000
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    direct_llm_timeout_sec: float = 90.0
    cosmic_memory_url: str = ""
    cosmic_memory_timeout_sec: float = 12.0
    cosmic_memory_core_fact_max_chars: int = 1500
    cosmic_memory_passive_max_results: int = 8
    cosmic_memory_passive_token_budget: int = 12000
    cosmic_memory_passive_kinds: tuple[str, ...] = (
        "session_summary",
        "task_summary",
        "agent_note",
        "user_data",
    )
    cosmic_memory_ingest_transcripts: bool = True
    cosmic_memory_episode_extract_graph: bool = False
    memory_write_max_per_hour: int = 50
    memory_write_dedup_ttl_sec: int = 86_400
    session_summary_max_output_tokens: int = 2500

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=_env_int("GATEWAY_PORT", 8080),
            public_host=os.getenv("GATEWAY_PUBLIC_HOST", "").strip(),
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
            redis_url=os.getenv("REDIS_URL", "").strip(),
            task_input_requests_stream=os.getenv("TASK_INPUT_REQUESTS_STREAM", "user_input:requests").strip()
            or "user_input:requests",
            task_input_replies_stream=os.getenv("TASK_INPUT_REPLIES_STREAM", "user_input:replies").strip()
            or "user_input:replies",
            task_input_gateway_group=os.getenv("TASK_INPUT_GATEWAY_GROUP", "gateway").strip() or "gateway",
            enable_whatsapp=_env_bool("WHATSAPP_ENABLED", True),
            enable_telegram=_env_bool("TELEGRAM_ENABLED", False),
            sessions_db_path=Path(
                os.getenv("GATEWAY_SESSIONS_DB_PATH", str(BACKEND_ROOT / "gateway" / "sessions.db"))
            ).expanduser(),
            routing_audit_db_path=Path(
                os.getenv(
                    "GATEWAY_ROUTING_AUDIT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "routing_audit.db"),
                )
            ).expanduser(),
            memory_write_audit_db_path=Path(
                os.getenv(
                    "GATEWAY_MEMORY_WRITE_AUDIT_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "memory_write_audit.db"),
                )
            ).expanduser(),
            artifacts_db_path=Path(
                os.getenv(
                    "GATEWAY_ARTIFACTS_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "artifacts.db"),
                )
            ).expanduser(),
            delivery_queue_db_path=Path(
                os.getenv(
                    "GATEWAY_DELIVERY_QUEUE_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "delivery_queue.db"),
                )
            ).expanduser(),
            scheduler_db_path=Path(
                os.getenv(
                    "GATEWAY_SCHEDULER_DB_PATH",
                    str(BACKEND_ROOT / "gateway" / "scheduler.db"),
                )
            ).expanduser(),
            session_transcript_dir=Path(
                os.getenv(
                    "GATEWAY_SESSION_TRANSCRIPT_DIR",
                    str(BACKEND_ROOT / "logs" / "sessions"),
                )
            ).expanduser(),
            session_reset_hour=min(
                23,
                max(0, _env_int("SESSION_RESET_HOUR", 4)),
            ),
            user_timezone_fallback=os.getenv("USER_TIMEZONE_FALLBACK", "America/Chicago").strip() or "America/Chicago",
            scheduler_poll_interval_sec=max(
                5.0,
                _env_float("GATEWAY_SCHEDULER_POLL_INTERVAL_SEC", 30.0),
            ),
            delivery_retry_base_sec=max(
                0.25,
                _env_float("GATEWAY_DELIVERY_RETRY_BASE_SEC", 1.0),
            ),
            delivery_retry_max_sec=max(
                1.0,
                _env_float("GATEWAY_DELIVERY_RETRY_MAX_SEC", 120.0),
            ),
            delivery_max_attempts=max(
                1,
                _env_int("GATEWAY_DELIVERY_MAX_ATTEMPTS", 12),
            ),
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
            cosmic_memory_url=os.getenv("COSMIC_MEMORY_URL", "").rstrip("/"),
            cosmic_memory_timeout_sec=max(
                1.0,
                _env_float("COSMIC_MEMORY_TIMEOUT_SEC", 12.0),
            ),
            cosmic_memory_core_fact_max_chars=max(
                250,
                _env_int("COSMIC_MEMORY_CORE_FACT_MAX_CHARS", 1500),
            ),
            cosmic_memory_passive_max_results=max(
                1,
                _env_int("COSMIC_MEMORY_PASSIVE_MAX_RESULTS", 8),
            ),
            cosmic_memory_passive_token_budget=max(
                256,
                _env_int("COSMIC_MEMORY_PASSIVE_TOKEN_BUDGET", 12000),
            ),
            cosmic_memory_passive_kinds=_env_csv(
                "COSMIC_MEMORY_PASSIVE_KINDS",
                (
                    "session_summary",
                    "task_summary",
                    "agent_note",
                    "user_data",
                ),
            ),
            cosmic_memory_ingest_transcripts=_env_bool("COSMIC_MEMORY_INGEST_TRANSCRIPTS", True),
            cosmic_memory_episode_extract_graph=_env_bool(
                "COSMIC_MEMORY_EPISODE_EXTRACT_GRAPH",
                False,
            ),
            memory_write_max_per_hour=max(
                1,
                _env_int("GATEWAY_MEMORY_WRITE_MAX_PER_HOUR", 50),
            ),
            memory_write_dedup_ttl_sec=max(
                60,
                _env_int("GATEWAY_MEMORY_WRITE_DEDUP_TTL_SEC", 86_400),
            ),
            session_summary_max_output_tokens=max(
                512,
                _env_int("GATEWAY_SESSION_SUMMARY_MAX_OUTPUT_TOKENS", 2500),
            ),
        )
