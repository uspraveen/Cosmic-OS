"""Diagram Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["DiagramAgentConfig", "AGENT_ROOT", "BACKEND_ROOT"]

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
class DiagramAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    # Internal LLM (gpt-5-mini)
    internal_llm_api_key: str = ""
    internal_llm_base_url: str = ""
    internal_llm_model: str = "gpt-5-mini"
    internal_llm_timeout_sec: float = 120.0
    enable_internal_llm: bool = True
    # LangGraph
    diagram_use_langgraph: bool = True
    diagram_max_tool_rounds: int = 6
    # Renderer paths (CLI binaries)
    mmdc_path: str = "mmdc"
    d2_path: str = "d2"
    # Rendering defaults
    default_format: str = "svg"
    default_theme: str = "default"
    mermaid_background: str = "white"
    mermaid_disable_sandbox: bool = True
    d2_sketch: bool = False
    d2_pad: int = 100
    excalidraw_theme: str = "hand-drawn"
    # Output
    output_max_width_px: int = 2400
    output_max_height_px: int = 1600
    # Artifacts
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"

    @classmethod
    def from_env(cls) -> "DiagramAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            internal_llm_api_key=(
                os.getenv("DIAGRAM_AGENT_INTERNAL_LLM_API_KEY")
                or os.getenv("OPENAI_COMPAT_API_KEY")
                or ""
            ).strip(),
            internal_llm_base_url=_normalize_openai_base_url(
                (
                    os.getenv("DIAGRAM_AGENT_INTERNAL_LLM_BASE_URL")
                    or os.getenv("OPENAI_COMPAT_BASE_URL")
                    or ""
                ).strip()
            ),
            internal_llm_model=(os.getenv("DIAGRAM_AGENT_INTERNAL_LLM_MODEL") or "gpt-5-mini").strip()
            or "gpt-5-mini",
            internal_llm_timeout_sec=_env_float("DIAGRAM_AGENT_INTERNAL_LLM_TIMEOUT_SEC", 120.0),
            enable_internal_llm=_env_bool("DIAGRAM_AGENT_ENABLE_INTERNAL_LLM", True),
            diagram_use_langgraph=_env_bool("DIAGRAM_AGENT_USE_LANGGRAPH", True),
            diagram_max_tool_rounds=max(
                1, _env_int("DIAGRAM_AGENT_MAX_TOOL_ROUNDS", 6)
            ),
            mmdc_path=os.getenv("DIAGRAM_AGENT_MMDC_PATH", "mmdc").strip() or "mmdc",
            d2_path=os.getenv("DIAGRAM_AGENT_D2_PATH", "d2").strip() or "d2",
            default_format=os.getenv("DIAGRAM_AGENT_DEFAULT_FORMAT", "svg").strip()
            or "svg",
            default_theme=os.getenv("DIAGRAM_AGENT_DEFAULT_THEME", "default").strip()
            or "default",
            mermaid_background=os.getenv("DIAGRAM_AGENT_MERMAID_BG", "white").strip()
            or "white",
            mermaid_disable_sandbox=_env_bool(
                "DIAGRAM_AGENT_MERMAID_DISABLE_SANDBOX", True
            ),
            d2_sketch=_env_bool("DIAGRAM_AGENT_D2_SKETCH", False),
            d2_pad=max(0, _env_int("DIAGRAM_AGENT_D2_PAD", 100)),
            output_max_width_px=max(200, _env_int("DIAGRAM_AGENT_MAX_WIDTH_PX", 2400)),
            output_max_height_px=max(
                200, _env_int("DIAGRAM_AGENT_MAX_HEIGHT_PX", 1600)
            ),
            artifacts_root=Path(
                os.getenv(
                    "DIAGRAM_AGENT_ARTIFACTS_ROOT",
                    str(BACKEND_ROOT / "runs" / "artifacts"),
                )
            ).expanduser(),
        )
