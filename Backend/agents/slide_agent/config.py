"""Slide Agent configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["SlideAgentConfig", "AGENT_ROOT", "BACKEND_ROOT"]

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
class SlideAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    # Internal LLM (gpt-5-mini)
    mimo_api_key: str = ""
    mimo_base_url: str = ""
    mimo_model: str = "gpt-5-mini"
    mimo_timeout_sec: float = 120.0
    enable_internal_llm: bool = True
    # LangGraph
    slide_use_langgraph: bool = True
    slide_max_tool_rounds: int = 10
    slide_max_doc_context_requests: int = 2
    # Templates
    templates_dir: Path = AGENT_ROOT / "templates"
    default_template: str = "corporate-dark"
    # Rendering
    libreoffice_path: str = "soffice"
    pdftoppm_path: str = "pdftoppm"
    render_dpi: int = 200
    # Output
    default_slide_width_inches: float = 13.333
    default_slide_height_inches: float = 7.5
    export_pdf: bool = True
    max_slides: int = 50
    # Validation
    max_validation_attempts: int = 2
    # Artifacts
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"
    # Image generation delegation
    image_agent_id: str = "cosmic/image-generator-agent:1.0.0"
    diagram_agent_id: str = "cosmic/diagram-agent:1.0.0"
    docs_parser_agent_id: str = "cosmic/docs-parser-agent:1.0.0"

    @classmethod
    def from_env(cls) -> "SlideAgentConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip(),
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            mimo_api_key=(
                os.getenv("SLIDE_AGENT_MIMO_API_KEY") or os.getenv("MIMO_API_KEY") or ""
            ).strip(),
            mimo_base_url=_normalize_openai_base_url(
                (
                    os.getenv("SLIDE_AGENT_MIMO_BASE_URL")
                    or os.getenv("MIMO_OPENAI_BASE_URL")
                    or ""
                ).strip()
            ),
            mimo_model=(os.getenv("SLIDE_AGENT_MIMO_MODEL") or "gpt-5-mini").strip()
            or "gpt-5-mini",
            mimo_timeout_sec=_env_float("SLIDE_AGENT_MIMO_TIMEOUT_SEC", 120.0),
            enable_internal_llm=_env_bool("SLIDE_AGENT_ENABLE_INTERNAL_LLM", True),
            slide_use_langgraph=_env_bool("SLIDE_AGENT_USE_LANGGRAPH", True),
            slide_max_tool_rounds=max(1, _env_int("SLIDE_AGENT_MAX_TOOL_ROUNDS", 10)),
            slide_max_doc_context_requests=max(
                1, _env_int("SLIDE_AGENT_MAX_DOC_CONTEXT_REQUESTS", 2)
            ),
            templates_dir=Path(
                os.getenv("SLIDE_AGENT_TEMPLATES_DIR", str(AGENT_ROOT / "templates"))
            ).expanduser(),
            default_template=os.getenv(
                "SLIDE_AGENT_DEFAULT_TEMPLATE", "corporate-dark"
            ).strip()
            or "corporate-dark",
            libreoffice_path=os.getenv(
                "SLIDE_AGENT_LIBREOFFICE_PATH", "soffice"
            ).strip()
            or "soffice",
            pdftoppm_path=os.getenv("SLIDE_AGENT_PDFTOPPM_PATH", "pdftoppm").strip()
            or "pdftoppm",
            render_dpi=max(72, _env_int("SLIDE_AGENT_RENDER_DPI", 200)),
            default_slide_width_inches=_env_float("SLIDE_AGENT_WIDTH_INCHES", 13.333),
            default_slide_height_inches=_env_float("SLIDE_AGENT_HEIGHT_INCHES", 7.5),
            export_pdf=_env_bool("SLIDE_AGENT_EXPORT_PDF", True),
            max_slides=max(1, _env_int("SLIDE_AGENT_MAX_SLIDES", 50)),
            max_validation_attempts=max(
                1, _env_int("SLIDE_AGENT_MAX_VALIDATION_ATTEMPTS", 2)
            ),
            artifacts_root=Path(
                os.getenv(
                    "SLIDE_AGENT_ARTIFACTS_ROOT",
                    str(BACKEND_ROOT / "runs" / "artifacts"),
                )
            ).expanduser(),
            docs_parser_agent_id=(
                os.getenv("SLIDE_AGENT_DOCS_PARSER_AGENT_ID")
                or "cosmic/docs-parser-agent:1.0.0"
            ).strip()
            or "cosmic/docs-parser-agent:1.0.0",
        )
