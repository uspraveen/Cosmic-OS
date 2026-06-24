from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "visual_enhancement.env")
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


def _env_optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value > 0 else None


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


def _normalize_openai_like_base_url(raw: str, *, default: str = "") -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return default
    for suffix in (
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/responses",
        "/responses",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    if not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


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
    orchestrator_default_provider: str = "fireworks_glm"
    fireworks_api_key: str = ""
    fireworks_base_url: str = "https://api.fireworks.ai/inference/v1"
    fireworks_kimi_model: str = "accounts/fireworks/models/kimi-k2p6"
    fireworks_glm_model: str = "accounts/fireworks/models/glm-5p2"
    fireworks_vision_fallback_model: str = "accounts/fireworks/models/kimi-k2p6"
    fireworks_kimi_max_tokens: int | None = None
    fireworks_kimi_temperature: float = 0.6
    local_code_execution_enabled: bool = True
    local_code_execution_timeout_sec: float = 45.0
    local_code_execution_allow_network: bool = False
    local_code_execution_allow_pip: bool = True
    local_code_execution_pip_timeout_sec: float = 120.0
    local_code_execution_venv_cache_root: Path | None = None
    local_code_execution_max_script_bytes: int = 256000
    local_code_execution_max_files: int = 12
    local_code_execution_max_file_bytes: int = 25 * 1024 * 1024
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
    heartbeat_notes_path: Path = BACKEND_ROOT / "agents" / "orchestrator" / "store" / "heartbeat_notes.md"
    # Tool executor service endpoints
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar"
    cosmic_memory_url: str = "http://127.0.0.1:8090"
    gateway_url: str = "http://127.0.0.1:8080"
    max_tool_iterations: int = 25
    visual_enhancement_enabled: bool = True
    visual_max_visuals_per_turn: int = 5
    visual_max_image_slots_per_turn: int = 5
    visual_max_chart_slots_per_turn: int = 1
    visual_max_concurrent_sidecars: int = 2
    visual_image_slot_timeout_ms: int = 6000
    visual_chart_slot_timeout_ms: int = 4000
    visual_finalization_grace_ms: int = 750
    visual_image_source_page_limit: int = 3
    visual_image_candidate_limit: int = 24
    visual_image_max_bytes: int = 8 * 1024 * 1024
    visual_image_verify_top_k: int = 3
    visual_image_min_confidence: float = 0.58
    visual_chart_max_points: int = 200
    visual_chart_max_bytes: int = 4 * 1024 * 1024
    visual_download_timeout_sec: float = 6.0
    visual_firecrawl_api_key: str = ""
    visual_firecrawl_base_url: str = "https://api.firecrawl.dev"
    visual_firecrawl_request_timeout_sec: float = 20.0
    visual_image_search_enabled: bool = True
    visual_image_search_base_url: str = "https://www.bing.com/images/search"
    visual_image_search_timeout_sec: float = 5.0
    visual_image_search_result_limit: int = 12
    visual_fireworks_api_key: str = ""
    visual_fireworks_base_url: str = "https://api.fireworks.ai/inference/v1"
    visual_fireworks_model: str = "accounts/fireworks/models/kimi-k2p6"
    visual_fireworks_vision_model: str = "accounts/fireworks/models/kimi-k2p6"
    visual_fireworks_reasoning_effort: str = "low"
    visual_fireworks_timeout_sec: float = 20.0

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
            orchestrator_default_provider=(
                os.getenv("COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER", "fireworks_glm").strip().lower()
                or "fireworks_glm"
            ),
            fireworks_api_key=(
                os.getenv("ORCHESTRATOR_FIREWORKS_API_KEY")
                or os.getenv("FIREWORKS_API_KEY")
                or os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY")
                or os.getenv("MODEL_API_KEY")
                or os.getenv("SLIDE_AGENT_FIREWORKS_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            fireworks_base_url=_normalize_openai_like_base_url(
                (
                    os.getenv("ORCHESTRATOR_FIREWORKS_BASE_URL")
                    or os.getenv("FIREWORKS_BASE_URL")
                    or os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL")
                    or os.getenv("MODEL_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or "https://api.fireworks.ai/inference/v1"
                ).strip(),
                default="https://api.fireworks.ai/inference/v1",
            ),
            fireworks_kimi_model=(
                os.getenv("ORCHESTRATOR_FIREWORKS_KIMI_MODEL")
                or os.getenv("FIREWORKS_KIMI_MODEL")
                or "accounts/fireworks/models/kimi-k2p6"
            ).strip()
            or "accounts/fireworks/models/kimi-k2p6",
            fireworks_glm_model=(
                os.getenv("ORCHESTRATOR_FIREWORKS_GLM_MODEL")
                or os.getenv("FIREWORKS_GLM_MODEL")
                or "accounts/fireworks/models/glm-5p2"
            ).strip()
            or "accounts/fireworks/models/glm-5p2",
            fireworks_vision_fallback_model=(
                os.getenv("ORCHESTRATOR_FIREWORKS_VISION_FALLBACK_MODEL")
                or os.getenv("ORCHESTRATOR_FIREWORKS_KIMI_MODEL")
                or os.getenv("FIREWORKS_KIMI_MODEL")
                or "accounts/fireworks/models/kimi-k2p6"
            ).strip()
            or "accounts/fireworks/models/kimi-k2p6",
            fireworks_kimi_max_tokens=_env_optional_positive_int("ORCHESTRATOR_FIREWORKS_MAX_TOKENS"),
            fireworks_kimi_temperature=max(
                0.0,
                min(2.0, _env_float("ORCHESTRATOR_FIREWORKS_TEMPERATURE", 0.6)),
            ),
            local_code_execution_enabled=_env_bool("ORCHESTRATOR_CODE_SANDBOX_ENABLED", True),
            local_code_execution_timeout_sec=max(
                1.0,
                _env_float("ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC", 45.0),
            ),
            local_code_execution_allow_network=_env_bool("ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK", False),
            local_code_execution_allow_pip=_env_bool("ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP", True),
            local_code_execution_pip_timeout_sec=max(
                10.0,
                _env_float("ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC", 120.0),
            ),
            local_code_execution_venv_cache_root=(
                Path(os.getenv("ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT", "")).expanduser()
                if os.getenv("ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT", "").strip()
                else None
            ),
            local_code_execution_max_script_bytes=max(
                4096,
                _env_int("ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES", 256000),
            ),
            local_code_execution_max_files=max(0, _env_int("ORCHESTRATOR_CODE_SANDBOX_MAX_FILES", 12)),
            local_code_execution_max_file_bytes=max(
                1024,
                _env_int("ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES", 25 * 1024 * 1024),
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
            heartbeat_notes_path=Path(
                os.getenv(
                    "COSMIC_HEARTBEAT_NOTES_PATH",
                    str(BACKEND_ROOT / "agents" / "orchestrator" / "store" / "heartbeat_notes.md"),
                )
            ).expanduser(),
            perplexity_api_key=os.getenv("PERPLEXITY_API_KEY", "").strip(),
            perplexity_model=os.getenv("PERPLEXITY_MODEL", "sonar").strip() or "sonar",
            cosmic_memory_url=os.getenv("COSMIC_MEMORY_URL", "http://127.0.0.1:8090").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            max_tool_iterations=max(1, _env_int("ORCHESTRATOR_MAX_TOOL_ITERATIONS", 25)),
            visual_enhancement_enabled=_env_bool("VISUAL_ENHANCEMENT_ENABLED", True),
            visual_max_visuals_per_turn=max(0, _env_int("VISUAL_ENHANCEMENT_MAX_VISUALS_PER_TURN", 5)),
            visual_max_image_slots_per_turn=max(0, _env_int("VISUAL_ENHANCEMENT_MAX_IMAGE_SLOTS_PER_TURN", 5)),
            visual_max_chart_slots_per_turn=max(0, _env_int("VISUAL_ENHANCEMENT_MAX_CHART_SLOTS_PER_TURN", 1)),
            visual_max_concurrent_sidecars=max(1, _env_int("VISUAL_ENHANCEMENT_MAX_CONCURRENT_SIDECARS", 2)),
            visual_image_slot_timeout_ms=max(250, _env_int("VISUAL_ENHANCEMENT_IMAGE_SLOT_TIMEOUT_MS", 6000)),
            visual_chart_slot_timeout_ms=max(250, _env_int("VISUAL_ENHANCEMENT_CHART_SLOT_TIMEOUT_MS", 4000)),
            visual_finalization_grace_ms=max(0, _env_int("VISUAL_ENHANCEMENT_FINALIZATION_GRACE_MS", 750)),
            visual_image_source_page_limit=max(1, _env_int("VISUAL_ENHANCEMENT_IMAGE_SOURCE_PAGE_LIMIT", 3)),
            visual_image_candidate_limit=max(1, _env_int("VISUAL_ENHANCEMENT_IMAGE_CANDIDATE_LIMIT", 24)),
            visual_image_max_bytes=max(1024, _env_int("VISUAL_ENHANCEMENT_IMAGE_MAX_BYTES", 8 * 1024 * 1024)),
            visual_image_verify_top_k=max(1, _env_int("VISUAL_ENHANCEMENT_IMAGE_VERIFY_TOP_K", 3)),
            visual_image_min_confidence=max(
                0.0,
                min(1.0, _env_float("VISUAL_ENHANCEMENT_IMAGE_MIN_CONFIDENCE", 0.58)),
            ),
            visual_chart_max_points=max(2, _env_int("VISUAL_ENHANCEMENT_CHART_MAX_POINTS", 200)),
            visual_chart_max_bytes=max(1024, _env_int("VISUAL_ENHANCEMENT_CHART_MAX_BYTES", 4 * 1024 * 1024)),
            visual_download_timeout_sec=max(5.0, _env_float("VISUAL_ENHANCEMENT_DOWNLOAD_TIMEOUT_SEC", 6.0)),
            visual_firecrawl_api_key=(
                os.getenv("VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY")
                or os.getenv("FIRECRAWL_API_KEY")
                or ""
            ).strip(),
            visual_firecrawl_base_url=(
                os.getenv("VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL")
                or os.getenv("FIRECRAWL_API_BASE_URL")
                or "https://api.firecrawl.dev"
            ).strip()
            or "https://api.firecrawl.dev",
            visual_firecrawl_request_timeout_sec=max(
                5.0,
                _env_float(
                    "VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC",
                    _env_float("FIRECRAWL_REQUEST_TIMEOUT_SEC", 20.0),
                ),
            ),
            visual_image_search_enabled=_env_bool("VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED", True),
            visual_image_search_base_url=(
                os.getenv("VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL")
                or "https://www.bing.com/images/search"
            ).strip()
            or "https://www.bing.com/images/search",
            visual_image_search_timeout_sec=max(
                3.0,
                _env_float("VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC", 5.0),
            ),
            visual_image_search_result_limit=max(
                1,
                _env_int("VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT", 12),
            ),
            visual_fireworks_api_key=(
                os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY")
                or os.getenv("MODEL_API_KEY")
                or os.getenv("FIREWORKS_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            visual_fireworks_base_url=_normalize_openai_like_base_url(
                (
                    os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL")
                    or os.getenv("MODEL_BASE_URL")
                    or os.getenv("FIREWORKS_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or "https://api.fireworks.ai/inference/v1"
                ).strip(),
                default="https://api.fireworks.ai/inference/v1",
            ),
            visual_fireworks_model=(
                os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_MODEL")
                or os.getenv("FIREWORKS_KIMI_MODEL")
                or "accounts/fireworks/models/kimi-k2p6"
            ).strip()
            or "accounts/fireworks/models/kimi-k2p6",
            visual_fireworks_vision_model=(
                os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL")
                or os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_MODEL")
                or os.getenv("FIREWORKS_KIMI_MODEL")
                or "accounts/fireworks/models/kimi-k2p6"
            ).strip()
            or "accounts/fireworks/models/kimi-k2p6",
            visual_fireworks_reasoning_effort=(
                os.getenv("VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT")
                or "low"
            ).strip()
            or "low",
            visual_fireworks_timeout_sec=max(
                5.0,
                _env_float("VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC", 20.0),
            ),
        )
