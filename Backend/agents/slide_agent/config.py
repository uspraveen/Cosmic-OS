"""Slide Agent configuration for the COSMIC adapter around cosmic-slides-2."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent

__all__ = ["AGENT_ROOT", "BACKEND_ROOT", "SlideAgentConfig"]

# Env layering (most wins first):
#   1. process environment — on the VM this is the systemd EnvironmentFile
#      (/etc/cosmic/agents/slide-agent.env), which is THE runtime config.
#   2. agent.env — the single local-dev file (gitignored; copy from
#      agent.env.example). The core modules read THIS file too via the one
#      loader in llm_client — do not reintroduce a second dev env file.
#   3. Backend/.env — whole-backend dev defaults.
# load_dotenv never overrides values already in the process environment, so
# a deployed systemd value always beats a local file value.
load_dotenv(AGENT_ROOT / "agent.env")
load_dotenv(BACKEND_ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


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


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


@dataclass(slots=True)
class SlideAgentConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""

    model_base_url: str = "https://api.fireworks.ai/inference/v1"
    model_api_key: str = ""
    model_name: str = "accounts/fireworks/models/glm-5p3-flash"
    model_timeout_sec: int = 300
    model_http_retries: int = 3
    model_max_tokens: int = 16384
    html_model_max_tokens: int = 4096
    vision_model_name: str = "accounts/fireworks/models/glm-5p3-flash"

    libreoffice_path: str = "soffice"
    pdftoppm_path: str = "pdftoppm"
    max_slides: int = 50
    max_slides_per_deck: int = 50
    validate_outputs: bool = True
    force_catalog_default: bool = False
    catalog_parallelism: int = 5
    builder_parallelism: int = 2
    builder_max_repair_rounds: int = 2
    html_max_repair_rounds: int = 1
    html_render_timeout_ms: int = 45000
    html_viewport_width: int = 1440
    html_viewport_height: int = 900
    html_device_scale: float = 1.5
    default_workflow: str = ""
    docs_parser_agent_id: str = "cosmic/docs-parser-agent:1.0.0"

    templates_dir: Path = AGENT_ROOT / "templates"
    catalogs_dir: Path = AGENT_ROOT / "catalogs"
    assets_cache_dir: Path = AGENT_ROOT / "assets" / "cache"
    artifacts_root: Path = BACKEND_ROOT / "runs" / "artifacts"

    @classmethod
    def from_env(cls) -> "SlideAgentConfig":
        explicit_model_name = _first_env(
            "MODEL_NAME",
            "SLIDE_AGENT_FIREWORKS_MODEL",
            "FIREWORKS_KIMI_MODEL",
        )
        fireworks_api_key = _first_env(
            "MODEL_API_KEY",
            "SLIDE_AGENT_FIREWORKS_API_KEY",
            "FIREWORKS_API_KEY",
            "OPENAI_COMPAT_API_KEY",
            "OPENROUTER_API_KEY",
            # VM-wide Fireworks credentials the bootstrap may have rendered for
            # peers; the slide agent can ride the same account.
            "ORCHESTRATOR_FIREWORKS_API_KEY",
            "VISUAL_ENHANCEMENT_FIREWORKS_API_KEY",
        )
        openai_api_key = _first_env(
            "SLIDE_AGENT_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        )
        # Vendor-paired default: without an explicit model or Fireworks key, an
        # OpenAI key upgrades the agent to GPT-5.6 Terra (heavy slide work).
        # Otherwise the Fireworks Qwen defaults keep working untouched.
        use_openai = not explicit_model_name and not fireworks_api_key and bool(openai_api_key)
        if use_openai:
            model_name = explicit_model_name or "gpt-5.6-terra"
            model_base_url = _first_env(
                "MODEL_BASE_URL",
                "OPENAI_COMPAT_BASE_URL",
                default="https://api.openai.com/v1",
            ).rstrip("/")
            model_api_key = openai_api_key
        else:
            model_name = explicit_model_name or "accounts/fireworks/models/glm-5p3-flash"
            model_base_url = _first_env(
                "MODEL_BASE_URL",
                "SLIDE_AGENT_FIREWORKS_BASE_URL",
                "FIREWORKS_BASE_URL",
                "OPENAI_COMPAT_BASE_URL",
                "OPENROUTER_BASE_URL",
                default="https://api.fireworks.ai/inference/v1",
            ).rstrip("/")
            model_api_key = fireworks_api_key
        return cls(
            redis_url=_first_env("REDIS_URL", default="redis://127.0.0.1:6379/0"),
            gateway_url=_first_env("GATEWAY_URL", default="http://127.0.0.1:8080"),
            gateway_internal_token=_first_env("GATEWAY_INTERNAL_TOKEN", default=""),
            model_base_url=model_base_url,
            model_api_key=model_api_key,
            model_name=model_name,
            model_timeout_sec=_env_int("MODEL_TIMEOUT_SEC", _env_int("SLIDE_AGENT_FIREWORKS_TIMEOUT_SEC", 300)),
            model_http_retries=_env_int("MODEL_HTTP_RETRIES", 3),
            model_max_tokens=_env_int("MODEL_MAX_TOKENS", 16384),
            html_model_max_tokens=_env_int("HTML_MODEL_MAX_TOKENS", 4096),
            vision_model_name=_first_env("VISION_MODEL_NAME", default=model_name),
            libreoffice_path=_first_env("LIBREOFFICE_PATH", "SLIDE_AGENT_LIBREOFFICE_PATH", default="soffice"),
            pdftoppm_path=_first_env("PDFTOPPM_PATH", "SLIDE_AGENT_PDFTOPPM_PATH", default="pdftoppm"),
            max_slides=max(1, _env_int("SLIDE_AGENT_MAX_SLIDES", _env_int("MAX_SLIDES", 50))),
            max_slides_per_deck=max(1, _env_int("SLIDE_AGENT_MAX_SLIDES_PER_DECK", 50)),
            validate_outputs=_env_bool("SLIDE_AGENT_VALIDATE_OUTPUTS", True),
            force_catalog_default=_env_bool("SLIDE_AGENT_FORCE_CATALOG_DEFAULT", False),
            catalog_parallelism=max(1, _env_int("CATALOG_PARALLELISM", 5)),
            builder_parallelism=max(1, _env_int("BUILDER_PARALLELISM", 2)),
            builder_max_repair_rounds=max(0, _env_int("BUILDER_MAX_REPAIR_ROUNDS", 2)),
            html_max_repair_rounds=max(0, _env_int("HTML_MAX_REPAIR_ROUNDS", 1)),
            html_render_timeout_ms=max(1000, _env_int("HTML_RENDER_TIMEOUT_MS", 45000)),
            html_viewport_width=max(640, _env_int("HTML_VIEWPORT_WIDTH", 1440)),
            html_viewport_height=max(480, _env_int("HTML_VIEWPORT_HEIGHT", 900)),
            html_device_scale=max(0.5, _env_float("HTML_DEVICE_SCALE", 1.5)),
            default_workflow=_first_env("SLIDE_AGENT_DEFAULT_WORKFLOW", default="").lower(),
            docs_parser_agent_id=_first_env(
                "SLIDE_AGENT_DOCS_PARSER_AGENT_ID",
                default="cosmic/docs-parser-agent:1.0.0",
            ),
        )

    def apply_to_environment(self) -> None:
        """Expose COSMIC config under the env names expected by cosmic-slides-2."""
        values = {
            "MODEL_BASE_URL": self.model_base_url,
            "MODEL_API_KEY": self.model_api_key,
            "MODEL_NAME": self.model_name,
            "MODEL_TIMEOUT_SEC": str(self.model_timeout_sec),
            "MODEL_HTTP_RETRIES": str(self.model_http_retries),
            "MODEL_MAX_TOKENS": str(self.model_max_tokens),
            "HTML_MODEL_MAX_TOKENS": str(self.html_model_max_tokens),
            "VISION_MODEL_NAME": self.vision_model_name,
            "LIBREOFFICE_PATH": self.libreoffice_path,
            "PDFTOPPM_PATH": self.pdftoppm_path,
            "CATALOGS_DIR": "catalogs",
            "CATALOG_PARALLELISM": str(self.catalog_parallelism),
            "ASSETS_CACHE_DIR": "assets/cache",
            "BUILDER_PARALLELISM": str(self.builder_parallelism),
            "BUILDER_MAX_REPAIR_ROUNDS": str(self.builder_max_repair_rounds),
            "HTML_MAX_REPAIR_ROUNDS": str(self.html_max_repair_rounds),
            "HTML_RENDER_TIMEOUT_MS": str(self.html_render_timeout_ms),
            "HTML_VIEWPORT_WIDTH": str(self.html_viewport_width),
            "HTML_VIEWPORT_HEIGHT": str(self.html_viewport_height),
            "HTML_DEVICE_SCALE": str(self.html_device_scale),
            "MAX_SLIDES": str(self.max_slides),
            "SLIDE_AGENT_MAX_SLIDES_PER_DECK": str(self.max_slides_per_deck),
        }
        for name, value in values.items():
            os.environ[name] = str(value)
