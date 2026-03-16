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


@dataclass(slots=True)
class FirecrawlWebScrapeConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    firecrawl_api_key: str = ""
    firecrawl_api_base_url: str = "https://api.firecrawl.dev"
    firecrawl_request_timeout_sec: float = 120.0
    firecrawl_extract_poll_interval_sec: float = 2.0
    firecrawl_extract_max_wait_sec: float = 120.0

    @classmethod
    def from_env(cls) -> "FirecrawlWebScrapeConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip() or "redis://127.0.0.1:6379/0",
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip() or "http://127.0.0.1:8080",
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", "").strip(),
            firecrawl_api_base_url=os.getenv("FIRECRAWL_API_BASE_URL", "https://api.firecrawl.dev").strip()
            or "https://api.firecrawl.dev",
            firecrawl_request_timeout_sec=max(15.0, _env_float("FIRECRAWL_REQUEST_TIMEOUT_SEC", 120.0)),
            firecrawl_extract_poll_interval_sec=max(0.5, _env_float("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC", 2.0)),
            firecrawl_extract_max_wait_sec=max(15.0, _env_float("FIRECRAWL_EXTRACT_MAX_WAIT_SEC", 120.0)),
        )
