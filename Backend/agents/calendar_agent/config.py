"""Calendar Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["CalendarAgentConfig", "AGENT_ROOT", "BACKEND_ROOT"]

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _normalize_openai_base_url(raw: str) -> str:
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
class CalendarAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    # Internal LLM for natural language parsing (gpt-5.6-luna)
    internal_llm_api_key: str = ""
    internal_llm_base_url: str = ""
    internal_llm_model: str = "gpt-5.6-luna"
    internal_llm_reasoning_effort: str = "xhigh"
    internal_llm_timeout_sec: float = 120.0
    enable_internal_llm: bool = True
    calendar_use_langgraph: bool = True
    calendar_max_tool_rounds: int = 6
    # Calendar defaults
    default_timezone: str = "America/Chicago"
    working_hour_start: int = 9
    working_hour_end: int = 17
    default_event_duration_min: int = 30
    buffer_between_events_min: int = 15
    max_events_per_list: int = 50

    @classmethod
    def from_env(cls) -> "CalendarAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            internal_llm_api_key=(
                os.getenv("CALENDAR_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            internal_llm_base_url=_normalize_openai_base_url(
                (
                    os.getenv("CALENDAR_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or ""
                ).strip()
            ),
            internal_llm_model=(os.getenv("CALENDAR_AGENT_INTERNAL_LLM_MODEL") or "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            internal_llm_reasoning_effort=(
                os.getenv("CALENDAR_AGENT_INTERNAL_LLM_REASONING_EFFORT") or "xhigh"
            ).strip()
            or "xhigh",
            internal_llm_timeout_sec=_env_float("CALENDAR_AGENT_INTERNAL_LLM_TIMEOUT_SEC", 120.0),
            enable_internal_llm=_env_bool("CALENDAR_AGENT_ENABLE_INTERNAL_LLM", True),
            calendar_use_langgraph=_env_bool(
                "CALENDAR_AGENT_USE_LANGGRAPH",
                True,
            ),
            calendar_max_tool_rounds=max(
                3,
                _env_int("CALENDAR_AGENT_MAX_TOOL_ROUNDS", 6),
            ),
            default_timezone=os.getenv(
                "CALENDAR_AGENT_DEFAULT_TIMEZONE", "America/Chicago"
            ).strip()
            or "America/Chicago",
            working_hour_start=max(
                0, min(23, _env_int("CALENDAR_AGENT_WORKING_HOUR_START", 9))
            ),
            working_hour_end=max(
                1, min(24, _env_int("CALENDAR_AGENT_WORKING_HOUR_END", 17))
            ),
            default_event_duration_min=max(
                5, _env_int("CALENDAR_AGENT_DEFAULT_EVENT_DURATION_MIN", 30)
            ),
            buffer_between_events_min=max(0, _env_int("CALENDAR_AGENT_BUFFER_MIN", 15)),
            max_events_per_list=max(
                1, _env_int("CALENDAR_AGENT_MAX_EVENTS_PER_LIST", 50)
            ),
        )
