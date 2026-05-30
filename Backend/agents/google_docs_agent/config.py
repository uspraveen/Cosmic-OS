"""Google Docs Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = [
    "AGENT_ROOT",
    "BACKEND_ROOT",
    "GoogleDocsAgentConfig",
    "normalize_openai_compatible_base_url",
]

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


def normalize_openai_compatible_base_url(raw: str) -> str:
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
class GoogleDocsAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    internal_llm_api_key: str = ""
    internal_llm_base_url: str = ""
    internal_llm_model: str = "gpt-5-mini"
    internal_llm_timeout_sec: float = 120.0
    enable_internal_llm: bool = True
    request_timeout_sec: float = 30.0
    max_search_results: int = 10
    max_read_chars: int = 30000
    max_blocks: int = 200
    max_comments: int = 100

    @classmethod
    def from_env(cls) -> "GoogleDocsAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            internal_llm_api_key=(
                os.getenv("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("GMAIL_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("CALENDAR_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ).strip(),
            internal_llm_base_url=normalize_openai_compatible_base_url(
                (
                    os.getenv("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("GMAIL_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("CALENDAR_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or ("https://api.openai.com/v1" if os.getenv("OPENAI_API_KEY") else "")
                    or ""
                ).strip()
            ),
            internal_llm_model=(os.getenv("GOOGLE_DOCS_AGENT_INTERNAL_LLM_MODEL") or "gpt-5-mini").strip()
            or "gpt-5-mini",
            internal_llm_timeout_sec=max(
                10.0,
                _env_float("GOOGLE_DOCS_AGENT_INTERNAL_LLM_TIMEOUT_SEC", 120.0),
            ),
            enable_internal_llm=_env_bool("GOOGLE_DOCS_AGENT_ENABLE_INTERNAL_LLM", True),
            request_timeout_sec=max(
                5.0,
                _env_float("GOOGLE_DOCS_AGENT_REQUEST_TIMEOUT_SEC", 30.0),
            ),
            max_search_results=max(
                1,
                min(50, _env_int("GOOGLE_DOCS_AGENT_MAX_SEARCH_RESULTS", 10)),
            ),
            max_read_chars=max(
                1000,
                _env_int("GOOGLE_DOCS_AGENT_MAX_READ_CHARS", 30000),
            ),
            max_blocks=max(20, _env_int("GOOGLE_DOCS_AGENT_MAX_BLOCKS", 200)),
            max_comments=max(10, _env_int("GOOGLE_DOCS_AGENT_MAX_COMMENTS", 100)),
        )
