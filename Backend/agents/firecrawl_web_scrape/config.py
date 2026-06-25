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
    firecrawl_agent_poll_interval_sec: float = 3.0
    firecrawl_agent_max_wait_sec: float = 240.0
    # Inline excerpt budgets surfaced back to the orchestrator. Full bodies always
    # live in artifacts; these only bound what we echo inline so a long page (or a
    # data table further down the page) is not silently cut off in the model view.
    inline_markdown_chars: int = 12000
    inline_html_chars: int = 6000
    # Capture page screenshots as a real image artifact (image/png) so the
    # orchestrator's vision path (Kimi) can read image-locked tables/charts.
    screenshot_as_image_artifact: bool = True
    screenshot_download_timeout_sec: float = 30.0
    screenshot_max_bytes: int = 12_000_000
    # Default to full-page screenshots so content below the fold (e.g. benchmark
    # tables rendered as images) is captured, not just the top viewport.
    screenshot_full_page: bool = True
    screenshot_quality: int = 80
    screenshot_viewport_width: int = 1280

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
            firecrawl_agent_poll_interval_sec=max(1.0, _env_float("FIRECRAWL_AGENT_POLL_INTERVAL_SEC", 3.0)),
            firecrawl_agent_max_wait_sec=max(30.0, _env_float("FIRECRAWL_AGENT_MAX_WAIT_SEC", 240.0)),
            inline_markdown_chars=max(1000, _env_int("FIRECRAWL_INLINE_MARKDOWN_CHARS", 12000)),
            inline_html_chars=max(500, _env_int("FIRECRAWL_INLINE_HTML_CHARS", 6000)),
            screenshot_as_image_artifact=_env_bool("FIRECRAWL_SCREENSHOT_AS_IMAGE_ARTIFACT", True),
            screenshot_download_timeout_sec=max(5.0, _env_float("FIRECRAWL_SCREENSHOT_DOWNLOAD_TIMEOUT_SEC", 30.0)),
            screenshot_max_bytes=max(100_000, _env_int("FIRECRAWL_SCREENSHOT_MAX_BYTES", 12_000_000)),
            screenshot_full_page=_env_bool("FIRECRAWL_SCREENSHOT_FULL_PAGE", True),
            screenshot_quality=min(100, max(1, _env_int("FIRECRAWL_SCREENSHOT_QUALITY", 80))),
            screenshot_viewport_width=min(7680, max(320, _env_int("FIRECRAWL_SCREENSHOT_VIEWPORT_WIDTH", 1280))),
        )
