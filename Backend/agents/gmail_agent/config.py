"""Gmail Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["AGENT_ROOT", "BACKEND_ROOT", "GmailAgentConfig", "normalize_openai_compatible_base_url"]

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
class GmailAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    internal_llm_api_key: str = ""
    internal_llm_base_url: str = ""
    internal_llm_model: str = "gpt-5-mini"
    internal_llm_timeout_sec: float = 90.0
    enable_internal_llm: bool = True
    max_search_results: int = 10
    max_triage_messages: int = 12
    max_thread_messages: int = 40
    max_body_chars: int = 6000
    max_digest_items: int = 6
    auto_prefilter_high_confidence_noise: bool = True
    prefilter_confidence_threshold: float = 0.92
    gmail_watch_topic_name: str = ""
    gmail_watch_label_ids: str = "INBOX"
    gmail_watch_webhook_secret: str = ""

    @classmethod
    def from_env(cls) -> "GmailAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            internal_llm_api_key=(
                os.getenv("GMAIL_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            internal_llm_base_url=normalize_openai_compatible_base_url(
                (
                    os.getenv("GMAIL_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or ""
                ).strip()
            ),
            internal_llm_model=(os.getenv("GMAIL_AGENT_INTERNAL_LLM_MODEL") or "gpt-5-mini").strip()
            or "gpt-5-mini",
            internal_llm_timeout_sec=max(
                10.0, _env_float("GMAIL_AGENT_INTERNAL_LLM_TIMEOUT_SEC", 90.0)
            ),
            enable_internal_llm=_env_bool("GMAIL_AGENT_ENABLE_INTERNAL_LLM", True),
            max_search_results=max(1, _env_int("GMAIL_AGENT_MAX_SEARCH_RESULTS", 10)),
            max_triage_messages=max(1, _env_int("GMAIL_AGENT_MAX_TRIAGE_MESSAGES", 12)),
            max_thread_messages=max(1, _env_int("GMAIL_AGENT_MAX_THREAD_MESSAGES", 40)),
            max_body_chars=max(500, _env_int("GMAIL_AGENT_MAX_BODY_CHARS", 6000)),
            max_digest_items=max(1, _env_int("GMAIL_AGENT_MAX_DIGEST_ITEMS", 6)),
            auto_prefilter_high_confidence_noise=_env_bool(
                "GMAIL_AGENT_AUTO_PREFILTER_HIGH_CONFIDENCE_NOISE", True
            ),
            prefilter_confidence_threshold=max(
                0.0,
                min(1.0, _env_float("GMAIL_AGENT_PREFILTER_CONFIDENCE_THRESHOLD", 0.92)),
            ),
            gmail_watch_topic_name=os.getenv("GMAIL_WATCH_TOPIC_NAME", "").strip(),
            gmail_watch_label_ids=os.getenv("GMAIL_WATCH_LABEL_IDS", "INBOX").strip() or "INBOX",
            gmail_watch_webhook_secret=os.getenv("GMAIL_WEBHOOK_SECRET", "").strip(),
        )
