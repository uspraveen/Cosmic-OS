from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_ROOT / "gateway.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass(slots=True)
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    local_api_token: str = ""
    internal_token: str = ""
    model_router_url: str = "http://127.0.0.1:8742"
    model_router_timeout_sec: float = 15.0
    enable_whatsapp: bool = True

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            port=_env_int("GATEWAY_PORT", 8080),
            local_api_token=(
                os.getenv("GATEWAY_LOCAL_API_TOKEN")
                or os.getenv("LOCAL_API_TOKEN")
                or ""
            ),
            internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", ""),
            model_router_url=os.getenv("MODEL_ROUTER_URL", "http://127.0.0.1:8742").rstrip("/"),
            model_router_timeout_sec=max(
                1.0,
                _env_float("MODEL_ROUTER_TIMEOUT_SEC", 15.0),
            ),
            enable_whatsapp=_env_bool("WHATSAPP_ENABLED", True),
        )
