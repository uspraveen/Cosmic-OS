from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent
load_dotenv(AGENT_ROOT / "agent.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class XTwitterSearchConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    xai_api_key: str = ""
    x_search_model: str = "grok-4.20-beta-0309-reasoning"
    x_search_max_output_tokens: int = 2200
    x_search_request_timeout_sec: float = 90.0
    x_search_max_posts: int = 8

    @classmethod
    def from_env(cls) -> "XTwitterSearchConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip() or "redis://127.0.0.1:6379/0",
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip() or "http://127.0.0.1:8080",
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            xai_api_key=os.getenv("XAI_API_KEY", "").strip(),
            x_search_model=os.getenv("X_SEARCH_MODEL", "grok-4.20-beta-0309-reasoning").strip()
            or "grok-4.20-beta-0309-reasoning",
            x_search_max_output_tokens=max(600, _env_int("X_SEARCH_MAX_OUTPUT_TOKENS", 2200)),
            x_search_request_timeout_sec=max(15.0, _env_float("X_SEARCH_REQUEST_TIMEOUT_SEC", 90.0)),
            x_search_max_posts=max(1, min(_env_int("X_SEARCH_MAX_POSTS", 8), 12)),
        )
