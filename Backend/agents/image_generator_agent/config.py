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
    "ImageGeneratorAgentConfig",
    "normalize_openai_like_base_url",
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


def normalize_openai_like_base_url(raw: str, *, default: str = "") -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return default
    for suffix in (
        "/v1/images/edits",
        "/images/edits",
        "/v1/images/generations",
        "/images/generations",
        "/v1/chat/completions",
        "/chat/completions",
    ):
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
            break
    if not value.endswith("/v1"):
        value = f"{value}/v1"
    return value


@dataclass(slots=True)
class ImageGeneratorAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""

    router_api_key: str = ""
    router_base_url: str = "https://api.openai.com/v1"
    router_model: str = "gpt-5-mini"
    router_timeout_sec: float = 45.0
    enable_internal_router_llm: bool = True

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_image_model: str = "gpt-image-1.5"
    openai_timeout_sec: float = 180.0

    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_image_model: str = "grok-imagine-image-pro"
    xai_timeout_sec: float = 180.0

    default_provider: str = "xai"
    default_size: str = "1024x1024"
    default_quality: str = "high"
    max_images_per_request: int = 4
    max_reference_images: int = 4
    max_prompt_chars: int = 6000
    generation_poll_interval_sec: float = 0.0

    @classmethod
    def from_env(cls) -> "ImageGeneratorAgentConfig":
        openai_base_url = normalize_openai_like_base_url(
            (os.getenv("IMAGE_AGENT_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip(),
            default="https://api.openai.com/v1",
        )
        router_base_url = normalize_openai_like_base_url(
            (os.getenv("IMAGE_AGENT_ROUTER_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip(),
            default=openai_base_url,
        )
        xai_base_url = normalize_openai_like_base_url(
            (os.getenv("IMAGE_AGENT_XAI_BASE_URL") or "").strip(),
            default="https://api.x.ai/v1",
        )
        default_provider = (os.getenv("IMAGE_AGENT_DEFAULT_PROVIDER", "xai").strip().lower() or "xai")
        if default_provider not in {"xai", "openai"}:
            default_provider = "xai"
        default_size = os.getenv("IMAGE_AGENT_DEFAULT_SIZE", "1024x1024").strip() or "1024x1024"
        if default_size not in {"1024x1024", "1024x1536", "1536x1024"}:
            default_size = "1024x1024"
        default_quality = os.getenv("IMAGE_AGENT_DEFAULT_QUALITY", "high").strip().lower() or "high"
        if default_quality not in {"auto", "low", "medium", "high"}:
            default_quality = "high"

        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            router_api_key=(os.getenv("IMAGE_AGENT_ROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
            router_base_url=router_base_url,
            router_model=os.getenv("IMAGE_AGENT_ROUTER_MODEL", "gpt-5-mini").strip() or "gpt-5-mini",
            router_timeout_sec=max(5.0, _env_float("IMAGE_AGENT_ROUTER_TIMEOUT_SEC", 45.0)),
            enable_internal_router_llm=_env_bool("IMAGE_AGENT_ENABLE_INTERNAL_ROUTER_LLM", True),
            openai_api_key=(os.getenv("IMAGE_AGENT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
            openai_base_url=openai_base_url,
            openai_image_model=os.getenv("IMAGE_AGENT_OPENAI_MODEL", "gpt-image-1.5").strip() or "gpt-image-1.5",
            openai_timeout_sec=max(10.0, _env_float("IMAGE_AGENT_OPENAI_TIMEOUT_SEC", 180.0)),
            xai_api_key=(os.getenv("IMAGE_AGENT_XAI_API_KEY") or os.getenv("XAI_API_KEY") or "").strip(),
            xai_base_url=xai_base_url,
            xai_image_model=os.getenv("IMAGE_AGENT_XAI_MODEL", "grok-imagine-image-pro").strip()
            or "grok-imagine-image-pro",
            xai_timeout_sec=max(10.0, _env_float("IMAGE_AGENT_XAI_TIMEOUT_SEC", 180.0)),
            default_provider=default_provider,
            default_size=default_size,
            default_quality=default_quality,
            max_images_per_request=max(1, _env_int("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST", 4)),
            max_reference_images=max(1, _env_int("IMAGE_AGENT_MAX_REFERENCE_IMAGES", 4)),
            max_prompt_chars=max(200, _env_int("IMAGE_AGENT_MAX_PROMPT_CHARS", 6000)),
            generation_poll_interval_sec=max(0.0, _env_float("IMAGE_AGENT_GENERATION_POLL_INTERVAL_SEC", 0.0)),
        )
