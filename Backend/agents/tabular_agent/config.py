from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["TabularAgentConfig", "AGENT_ROOT", "BACKEND_ROOT", "normalize_mimo_openai_base_url"]
load_dotenv(AGENT_ROOT / "agent.env")
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


def normalize_mimo_openai_base_url(raw: str) -> str:
    """
    MiMo (OpenAI-compatible) expects HTTP POST to ``{base}/chat/completions``.

    LangChain/OpenAI SDKs set ``base_url`` to the API root **including** ``/v1`` only.
    Do **not** include ``/chat/completions`` in the env var or requests become
    ``.../v1/chat/completions/chat/completions`` and the server returns 404.
    """
    s = str(raw or "").strip().rstrip("/")
    if not s:
        return ""
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].rstrip("/")
            break
    if not s.endswith("/v1"):
        s = f"{s}/v1"
    return s


@dataclass(slots=True)
class TabularAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    # For mid-task clarification: POST to orchestrator /internal/tasks/{parent_task_id}/request-input
    orchestrator_url: str = "http://127.0.0.1:8743"
    orchestrator_internal_token: str = ""
    max_input_artifacts: int = 8
    max_parallel_files: int = 3
    max_sheet_profile_parallelism: int = 6
    max_input_file_bytes: int = 25 * 1024 * 1024
    max_sheets_per_workbook: int = 100
    wide_column_warning_threshold: int = 200
    max_preview_rows: int = 30
    max_preview_columns: int = 40
    max_query_result_rows: int = 200
    sandbox_timeout_sec: float = 45.0
    sandbox_allow_network: bool = False
    sandbox_allow_pip: bool = False
    sandbox_pip_timeout_sec: float = 120.0
    sandbox_venv_cache_root: str = ""
    mimo_api_key: str = ""
    mimo_base_url: str = ""
    mimo_model: str = "mimo-v2-pro"
    mimo_timeout_sec: float = 120.0
    enable_internal_llm: bool = True
    include_financial_fpna_prompt: bool = False
    # Multi-step tabular.reason_workbook (LangGraph): max deterministic tool executions per task.
    tabular_reason_max_tool_rounds: int = 5
    # Use LangGraph multi-step loop when True; otherwise single-shot legacy path.
    tabular_reason_use_langgraph: bool = True
    # Blocking wait for ``user_input:replies`` (orchestrator request-input); cap for safety.
    tabular_reason_clarify_wait_sec: float = 600.0

    @classmethod
    def from_env(cls) -> "TabularAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            orchestrator_url=os.getenv(
                "TABULAR_AGENT_ORCHESTRATOR_URL",
                os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8743"),
            ).strip()
            or "http://127.0.0.1:8743",
            orchestrator_internal_token=(
                os.getenv("TABULAR_AGENT_ORCHESTRATOR_INTERNAL_TOKEN")
                or os.getenv("ORCHESTRATOR_INTERNAL_TOKEN")
                or ""
            ).strip(),
            max_input_artifacts=_env_int("TABULAR_AGENT_MAX_INPUT_ARTIFACTS", 8),
            max_parallel_files=_env_int("TABULAR_AGENT_MAX_PARALLEL_FILES", 3),
            max_sheet_profile_parallelism=_env_int("TABULAR_AGENT_MAX_SHEET_PROFILE_PARALLELISM", 6),
            max_input_file_bytes=_env_int("TABULAR_AGENT_MAX_INPUT_FILE_BYTES", 25 * 1024 * 1024),
            max_sheets_per_workbook=_env_int("TABULAR_AGENT_MAX_SHEETS", 100),
            wide_column_warning_threshold=_env_int("TABULAR_AGENT_WIDE_COL_WARN", 200),
            max_preview_rows=_env_int("TABULAR_AGENT_MAX_PREVIEW_ROWS", 30),
            max_preview_columns=_env_int("TABULAR_AGENT_MAX_PREVIEW_COLS", 40),
            max_query_result_rows=_env_int("TABULAR_AGENT_MAX_QUERY_ROWS", 200),
            sandbox_timeout_sec=_env_float("TABULAR_AGENT_SANDBOX_TIMEOUT_SEC", 45.0),
            sandbox_allow_network=os.getenv("TABULAR_AGENT_SANDBOX_ALLOW_NETWORK", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            sandbox_allow_pip=os.getenv("TABULAR_AGENT_SANDBOX_ALLOW_PIP", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            sandbox_pip_timeout_sec=_env_float("TABULAR_AGENT_SANDBOX_PIP_TIMEOUT_SEC", 120.0),
            sandbox_venv_cache_root=os.getenv("TABULAR_AGENT_SANDBOX_VENV_CACHE_ROOT", "").strip(),
            mimo_api_key=(
                os.getenv("TABULAR_AGENT_MIMO_API_KEY")
                or os.getenv("MIMO_API_KEY")
                or ""
            ).strip(),
            mimo_base_url=normalize_mimo_openai_base_url(
                (
                    os.getenv("TABULAR_AGENT_MIMO_BASE_URL")
                    or os.getenv("MIMO_OPENAI_BASE_URL")
                    or ""
                ).strip()
            ),
            mimo_model=os.getenv("TABULAR_AGENT_MIMO_MODEL", "mimo-v2-pro").strip() or "mimo-v2-pro",
            mimo_timeout_sec=_env_float("TABULAR_AGENT_MIMO_TIMEOUT_SEC", 120.0),
            enable_internal_llm=os.getenv("TABULAR_AGENT_ENABLE_INTERNAL_LLM", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            include_financial_fpna_prompt=os.getenv("TABULAR_AGENT_INCLUDE_FPAN_PROMPT", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            tabular_reason_max_tool_rounds=_env_int("TABULAR_AGENT_REASON_MAX_TOOL_ROUNDS", 5),
            tabular_reason_use_langgraph=os.getenv("TABULAR_AGENT_REASON_USE_LANGGRAPH", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            tabular_reason_clarify_wait_sec=_env_float("TABULAR_AGENT_REASON_CLARIFY_WAIT_SEC", 600.0),
        )
