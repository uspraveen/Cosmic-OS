from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "orchestrator.env")
load_dotenv(BACKEND_ROOT / ".env")


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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_json_map(name: str) -> dict[str, str]:
    raw = os.getenv(name)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in parsed.items():
        normalized_key = str(key or "").strip()
        normalized_value = str(value or "").strip()
        if normalized_key and normalized_value:
            result[normalized_key] = normalized_value
    return result


@dataclass(slots=True)
class OrchestratorConfig:
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"
    host: str = "127.0.0.1"
    port: int = 8743
    internal_token: str = ""
    signing_secret: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-6"
    anthropic_overload_retry_attempts: int = 1
    anthropic_overload_initial_backoff_sec: float = 1.0
    anthropic_overload_max_backoff_sec: float = 4.0
    anthropic_overload_fallback_model: str = ""
    anthropic_version: str = "2023-06-01"
    anthropic_files_api_beta: str = "files-api-2025-04-14"
    anthropic_code_execution_beta: str = "code-execution-2025-05-22"
    anthropic_prompt_cache_enabled: bool = False
    anthropic_max_input_images: int = 10
    anthropic_max_staged_input_files: int = 4
    anthropic_max_staged_input_file_bytes: int = 20 * 1024 * 1024
    max_tokens: int = 16000
    request_timeout_sec: float = 300.0
    redis_url: str = ""
    agent_registry_db_path: Path = BACKEND_ROOT / "registry" / "registry.db"
    agent_events_stream: str = "streams:events"
    agent_events_group: str = "orchestrator"
    orchestrator_agent_id: str = "cosmic/orchestrator:1.0.0"
    agent_signing_secrets: dict[str, str] = field(default_factory=dict)
    featured_specialists_enabled: bool = True
    featured_specialists_count: int = 5
    featured_specialists_lookback_days: int = 15
    featured_specialists_refresh_sec: int = 300
    task_input_requests_stream: str = "user_input:requests"
    task_input_replies_stream: str = "user_input:replies"
    task_input_orchestrator_group: str = "orchestrator"
    task_ledger_db_path: Path = BACKEND_ROOT / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"
    # Tool executor service endpoints
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    cosmic_memory_url: str = "http://127.0.0.1:8090"
    gateway_url: str = "http://127.0.0.1:8080"
    max_tool_iterations: int = 25

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        return cls(
            artifacts_root=Path(
                os.getenv("COSMIC_ARTIFACTS_ROOT", str(BACKEND_ROOT / "runs" / "artifacts"))
            ).expanduser(),
            host=os.getenv("ORCHESTRATOR_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("ORCHESTRATOR_PORT", 8743),
            internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            signing_secret=os.getenv("GATEWAY_SIGNING_SECRET", "").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6").strip(),
            anthropic_overload_retry_attempts=max(0, _env_int("ANTHROPIC_OVERLOAD_RETRY_ATTEMPTS", 1)),
            anthropic_overload_initial_backoff_sec=max(
                0.1,
                _env_float("ANTHROPIC_OVERLOAD_INITIAL_BACKOFF_SEC", 1.0),
            ),
            anthropic_overload_max_backoff_sec=max(
                0.1,
                _env_float("ANTHROPIC_OVERLOAD_MAX_BACKOFF_SEC", 4.0),
            ),
            anthropic_overload_fallback_model=os.getenv("ANTHROPIC_OVERLOAD_FALLBACK_MODEL", "").strip(),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01").strip(),
            anthropic_files_api_beta=os.getenv("ANTHROPIC_FILES_API_BETA", "files-api-2025-04-14").strip(),
            anthropic_code_execution_beta=os.getenv("ANTHROPIC_CODE_EXECUTION_BETA", "code-execution-2025-05-22").strip(),
            anthropic_prompt_cache_enabled=_env_bool("ANTHROPIC_PROMPT_CACHE_ENABLED", False),
            anthropic_max_input_images=max(1, _env_int("ANTHROPIC_MAX_INPUT_IMAGES", 10)),
            anthropic_max_staged_input_files=max(0, _env_int("ANTHROPIC_MAX_STAGED_INPUT_FILES", 4)),
            anthropic_max_staged_input_file_bytes=max(
                1024,
                _env_int("ANTHROPIC_MAX_STAGED_INPUT_FILE_BYTES", 20 * 1024 * 1024),
            ),
            max_tokens=max(256, _env_int("OPUS_MAX_TOKENS", 16000)),
            request_timeout_sec=max(30.0, _env_float("ORCHESTRATOR_REQUEST_TIMEOUT_SEC", 300.0)),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            agent_registry_db_path=Path(
                os.getenv("AGENT_REGISTRY_DB_PATH", str(BACKEND_ROOT / "registry" / "registry.db"))
            ).expanduser(),
            agent_events_stream=os.getenv("AGENT_EVENTS_STREAM", "streams:events").strip() or "streams:events",
            agent_events_group=os.getenv("AGENT_EVENTS_GROUP", "orchestrator").strip() or "orchestrator",
            orchestrator_agent_id=os.getenv("ORCHESTRATOR_AGENT_ID", "cosmic/orchestrator:1.0.0").strip()
            or "cosmic/orchestrator:1.0.0",
            agent_signing_secrets=_env_json_map("AGENT_SIGNING_SECRETS_JSON"),
            featured_specialists_enabled=_env_bool("ORCHESTRATOR_FEATURED_SPECIALISTS_ENABLED", True),
            featured_specialists_count=max(0, _env_int("ORCHESTRATOR_FEATURED_SPECIALISTS_COUNT", 5)),
            featured_specialists_lookback_days=max(1, _env_int("ORCHESTRATOR_FEATURED_SPECIALISTS_LOOKBACK_DAYS", 15)),
            featured_specialists_refresh_sec=max(30, _env_int("ORCHESTRATOR_FEATURED_SPECIALISTS_REFRESH_SEC", 300)),
            task_input_requests_stream=os.getenv("TASK_INPUT_REQUESTS_STREAM", "user_input:requests").strip()
            or "user_input:requests",
            task_input_replies_stream=os.getenv("TASK_INPUT_REPLIES_STREAM", "user_input:replies").strip()
            or "user_input:replies",
            task_input_orchestrator_group=os.getenv("TASK_INPUT_ORCHESTRATOR_GROUP", "orchestrator").strip()
            or "orchestrator",
            task_ledger_db_path=Path(
                os.getenv(
                    "ORCHESTRATOR_TASK_LEDGER_DB_PATH",
                    str(BACKEND_ROOT / "agents" / "orchestrator" / "store" / "data" / "task_ledger.db"),
                )
            ).expanduser(),
            perplexity_api_key=os.getenv("PERPLEXITY_API_KEY", "").strip(),
            perplexity_model=os.getenv("PERPLEXITY_MODEL", "sonar").strip() or "sonar",
            cosmic_memory_url=os.getenv("COSMIC_MEMORY_URL", "http://127.0.0.1:8090").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            max_tool_iterations=max(1, _env_int("ORCHESTRATOR_MAX_TOOL_ITERATIONS", 25)),
        )
