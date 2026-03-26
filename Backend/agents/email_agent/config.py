from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from shared import normalize_cosmic_mail_base_url


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["EmailAgentConfig", "AGENT_ROOT", "BACKEND_ROOT", "normalize_mimo_openai_base_url"]

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
class EmailAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    cosmic_mail_base_url: str = ""
    cosmic_mail_api_token: str = ""
    cosmic_mail_timeout_sec: float = 20.0
    primary_mailbox_address: str = ""
    max_search_results: int = 12
    max_thread_messages: int = 50
    max_attachment_downloads: int = 8
    max_attachment_bytes: int = 15 * 1024 * 1024
    mimo_api_key: str = ""
    mimo_base_url: str = ""
    mimo_model: str = "mimo-v2-pro"
    mimo_timeout_sec: float = 120.0
    enable_internal_llm: bool = True

    @classmethod
    def from_env(cls) -> "EmailAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            cosmic_mail_base_url=normalize_cosmic_mail_base_url(
                os.getenv("COSMIC_MAIL_BASE_URL", "").strip()
            ),
            cosmic_mail_api_token=os.getenv("COSMIC_MAIL_API_TOKEN", "").strip(),
            cosmic_mail_timeout_sec=_env_float("COSMIC_MAIL_TIMEOUT_SEC", 20.0),
            primary_mailbox_address=os.getenv("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS", "").strip(),
            max_search_results=max(1, _env_int("EMAIL_AGENT_MAX_SEARCH_RESULTS", 12)),
            max_thread_messages=max(1, _env_int("EMAIL_AGENT_MAX_THREAD_MESSAGES", 50)),
            max_attachment_downloads=max(0, _env_int("EMAIL_AGENT_MAX_ATTACHMENT_DOWNLOADS", 8)),
            max_attachment_bytes=max(0, _env_int("EMAIL_AGENT_MAX_ATTACHMENT_BYTES", 15 * 1024 * 1024)),
            mimo_api_key=(os.getenv("EMAIL_AGENT_MIMO_API_KEY") or os.getenv("MIMO_API_KEY") or "").strip(),
            mimo_base_url=normalize_mimo_openai_base_url(
                (os.getenv("EMAIL_AGENT_MIMO_BASE_URL") or os.getenv("MIMO_OPENAI_BASE_URL") or "").strip()
            ),
            mimo_model=os.getenv("EMAIL_AGENT_MIMO_MODEL", "mimo-v2-pro").strip() or "mimo-v2-pro",
            mimo_timeout_sec=_env_float("EMAIL_AGENT_MIMO_TIMEOUT_SEC", 120.0),
            enable_internal_llm=os.getenv("EMAIL_AGENT_ENABLE_INTERNAL_LLM", "true").strip().lower()
            not in {"0", "false", "no", "off"},
        )
