"""Map Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["MapAgentConfig", "AGENT_ROOT", "BACKEND_ROOT"]

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
class MapAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    osrm_base_url: str = "https://router.project-osrm.org"
    nominatim_user_agent: str = "Cosmic-OS/1.0 (map-agent; contact=support@cosmic.local)"
    geocode_timeout_sec: float = 20.0
    route_timeout_sec: float = 30.0
    max_markers: int = 24
    max_route_waypoints: int = 12
    internal_llm_api_key: str = ""
    internal_llm_base_url: str = ""
    internal_llm_model: str = "gpt-5.6-luna"
    internal_llm_reasoning_effort: str = "xhigh"
    internal_llm_timeout_sec: float = 90.0
    enable_internal_llm: bool = True
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"

    @classmethod
    def from_env(cls) -> "MapAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            nominatim_base_url=(
                os.getenv("MAP_AGENT_NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
            ).strip()
            or "https://nominatim.openstreetmap.org",
            osrm_base_url=(
                os.getenv("MAP_AGENT_OSRM_BASE_URL", "https://router.project-osrm.org")
            ).strip()
            or "https://router.project-osrm.org",
            nominatim_user_agent=(
                os.getenv("MAP_AGENT_NOMINATIM_USER_AGENT")
                or "Cosmic-OS/1.0 (map-agent; contact=support@cosmic.local)"
            ).strip(),
            geocode_timeout_sec=_env_float("MAP_AGENT_GEOCODE_TIMEOUT_SEC", 20.0),
            route_timeout_sec=_env_float("MAP_AGENT_ROUTE_TIMEOUT_SEC", 30.0),
            max_markers=max(1, _env_int("MAP_AGENT_MAX_MARKERS", 24)),
            max_route_waypoints=max(2, _env_int("MAP_AGENT_MAX_ROUTE_WAYPOINTS", 12)),
            internal_llm_api_key=(
                os.getenv("MAP_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            internal_llm_base_url=_normalize_openai_base_url(
                (
                    os.getenv("MAP_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or ""
                ).strip()
            ),
            internal_llm_model=(os.getenv("MAP_AGENT_INTERNAL_LLM_MODEL") or "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            internal_llm_reasoning_effort=(
                os.getenv("MAP_AGENT_INTERNAL_LLM_REASONING_EFFORT") or "xhigh"
            ).strip()
            or "xhigh",
            internal_llm_timeout_sec=_env_float("MAP_AGENT_INTERNAL_LLM_TIMEOUT_SEC", 90.0),
            enable_internal_llm=_env_bool("MAP_AGENT_ENABLE_INTERNAL_LLM", True),
            artifacts_root=Path(
                os.getenv(
                    "MAP_AGENT_ARTIFACTS_ROOT",
                    str(BACKEND_ROOT / "runs" / "artifacts"),
                )
            ).expanduser(),
        )
