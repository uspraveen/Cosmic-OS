from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


AGENT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = AGENT_ROOT.parent.parent
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


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None:
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


def _load_text(path: Path, default: str) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    return text or default


@dataclass(slots=True)
class DocsParserConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    max_input_artifacts: int = 8
    max_parallel_documents: int = 3
    max_input_file_bytes: int = 20 * 1024 * 1024
    max_num_pages: int = 200
    default_enable_ocr: bool = True
    default_generate_page_images: bool = False
    default_generate_picture_images: bool = True
    default_enable_picture_description: bool = True
    default_enable_full_page_vlm: bool = True
    default_enable_asset_reinspection: bool = True
    enable_office_render_fallback: bool = True
    office_renderer_path: str = "soffice"
    office_render_timeout_sec: float = 180.0
    picture_description_api_key: str = ""
    picture_description_api_url: str = "https://api.openai.com/v1/chat/completions"
    picture_description_model: str = "gpt-4.1-mini"
    picture_description_preset: str = "qwen"
    picture_description_timeout_sec: float = 90.0
    picture_description_concurrency: int = 8
    picture_description_batch_size: int = 4
    picture_description_max_new_tokens: int = 220
    picture_description_scale: float = 2.0
    picture_description_area_threshold: float = 0.05
    picture_description_classification_min_confidence: float = 0.2
    picture_description_classification_deny: tuple[str, ...] = (
        "logo",
        "icon",
        "signature",
        "stamp",
        "qr_code",
        "bar_code",
    )
    picture_description_prompt: str = _load_text(
        AGENT_ROOT / "prompts" / "picture_description.md",
        "Describe this document image for downstream question answering and synthesis. "
        "Focus on chart type, axes, labels, trends, visible text, diagram relationships, and any important values. "
        "Be concise and factual. If text is unreadable, say so.",
    )
    full_page_vlm_api_key: str = ""
    full_page_vlm_api_url: str = "https://api.openai.com/v1/chat/completions"
    full_page_vlm_model: str = "gpt-4.1-mini"
    full_page_vlm_preset: str = "qwen"
    full_page_vlm_timeout_sec: float = 120.0
    full_page_vlm_concurrency: int = 6
    full_page_vlm_batch_size: int = 2
    full_page_vlm_max_new_tokens: int = 1200
    full_page_vlm_scale: float = 2.0
    asset_reinspection_api_key: str = ""
    asset_reinspection_api_url: str = "https://api.openai.com/v1/chat/completions"
    asset_reinspection_model: str = "gpt-4.1-mini"
    asset_reinspection_timeout_sec: float = 90.0
    asset_reinspection_max_new_tokens: int = 900
    asset_reinspection_detail: str = "high"
    asset_reinspection_prompt: str = _load_text(
        AGENT_ROOT / "prompts" / "asset_reinspection.md",
        "Return a concise grounded JSON description of this document asset. "
        "Focus on chart type, labels, visible text, diagram relationships, layout hierarchy, and any important values or callouts.",
    )
    image_heavy_low_text_chars_per_unit: int = 160
    image_heavy_low_text_unit_ratio_threshold: float = 0.55
    image_heavy_visual_unit_ratio_threshold: float = 0.35
    image_heavy_chunk_density_threshold: float = 0.35
    image_heavy_very_low_avg_text_chars_threshold: int = 90
    max_chunk_chars: int = 2400
    chunk_overlap_chars: int = 280

    @classmethod
    def from_env(cls) -> "DocsParserConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip() or "redis://127.0.0.1:6379/0",
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip() or "http://127.0.0.1:8080",
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            max_input_artifacts=max(1, _env_int("DOCS_PARSER_MAX_INPUT_ARTIFACTS", 8)),
            max_parallel_documents=max(1, _env_int("DOCS_PARSER_MAX_PARALLEL_DOCUMENTS", 3)),
            max_input_file_bytes=max(1024 * 1024, _env_int("DOCS_PARSER_MAX_INPUT_FILE_BYTES", 20 * 1024 * 1024)),
            max_num_pages=max(1, _env_int("DOCS_PARSER_MAX_NUM_PAGES", 200)),
            default_enable_ocr=os.getenv("DOCS_PARSER_ENABLE_OCR", "true").strip().lower() not in {"0", "false", "no"},
            default_generate_page_images=os.getenv("DOCS_PARSER_GENERATE_PAGE_IMAGES", "false").strip().lower()
            in {"1", "true", "yes"},
            default_generate_picture_images=os.getenv("DOCS_PARSER_GENERATE_PICTURE_IMAGES", "true").strip().lower()
            not in {"0", "false", "no"},
            default_enable_picture_description=os.getenv("DOCS_PARSER_ENABLE_PICTURE_DESCRIPTION", "true").strip().lower()
            not in {"0", "false", "no"},
            default_enable_full_page_vlm=os.getenv("DOCS_PARSER_ENABLE_FULL_PAGE_VLM", "true").strip().lower()
            not in {"0", "false", "no"},
            default_enable_asset_reinspection=os.getenv("DOCS_PARSER_ENABLE_ASSET_REINSPECTION", "true").strip().lower()
            not in {"0", "false", "no"},
            enable_office_render_fallback=os.getenv("DOCS_PARSER_ENABLE_OFFICE_RENDER_FALLBACK", "true").strip().lower()
            not in {"0", "false", "no"},
            office_renderer_path=os.getenv("DOCS_PARSER_OFFICE_RENDERER_PATH", "soffice").strip() or "soffice",
            office_render_timeout_sec=max(
                10.0,
                _env_float("DOCS_PARSER_OFFICE_RENDER_TIMEOUT_SEC", 180.0),
            ),
            picture_description_api_key=(
                os.getenv("DOCS_PARSER_PICTURE_DESCRIPTION_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ).strip(),
            picture_description_api_url=(
                os.getenv("DOCS_PARSER_PICTURE_DESCRIPTION_API_URL", "https://api.openai.com/v1/chat/completions").strip()
                or "https://api.openai.com/v1/chat/completions"
            ),
            picture_description_model=(
                os.getenv("DOCS_PARSER_PICTURE_DESCRIPTION_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
            ),
            picture_description_preset=(
                os.getenv("DOCS_PARSER_PICTURE_DESCRIPTION_PRESET", "qwen").strip() or "qwen"
            ),
            picture_description_timeout_sec=max(
                10.0,
                _env_float("DOCS_PARSER_PICTURE_DESCRIPTION_TIMEOUT_SEC", 90.0),
            ),
            picture_description_concurrency=max(
                1,
                _env_int("DOCS_PARSER_PICTURE_DESCRIPTION_CONCURRENCY", 8),
            ),
            picture_description_batch_size=max(
                1,
                _env_int("DOCS_PARSER_PICTURE_DESCRIPTION_BATCH_SIZE", 4),
            ),
            picture_description_max_new_tokens=max(
                32,
                _env_int("DOCS_PARSER_PICTURE_DESCRIPTION_MAX_NEW_TOKENS", 220),
            ),
            picture_description_scale=max(
                0.5,
                _env_float("DOCS_PARSER_PICTURE_DESCRIPTION_SCALE", 2.0),
            ),
            picture_description_area_threshold=min(
                1.0,
                max(0.0, _env_float("DOCS_PARSER_PICTURE_DESCRIPTION_AREA_THRESHOLD", 0.05)),
            ),
            picture_description_classification_min_confidence=min(
                1.0,
                max(0.0, _env_float("DOCS_PARSER_PICTURE_DESCRIPTION_MIN_CONFIDENCE", 0.2)),
            ),
            picture_description_classification_deny=_env_csv(
                "DOCS_PARSER_PICTURE_DESCRIPTION_DENY_LABELS",
                ("logo", "icon", "signature", "stamp", "qr_code", "bar_code"),
            ),
            picture_description_prompt=_load_text(
                Path(
                    os.getenv(
                        "DOCS_PARSER_PICTURE_DESCRIPTION_PROMPT_PATH",
                        str(AGENT_ROOT / "prompts" / "picture_description.md"),
                    )
                ),
                cls.picture_description_prompt,
            ),
            full_page_vlm_api_key=(
                os.getenv("DOCS_PARSER_FULL_PAGE_VLM_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ).strip(),
            full_page_vlm_api_url=(
                os.getenv("DOCS_PARSER_FULL_PAGE_VLM_API_URL", "https://api.openai.com/v1/chat/completions").strip()
                or "https://api.openai.com/v1/chat/completions"
            ),
            full_page_vlm_model=(
                os.getenv("DOCS_PARSER_FULL_PAGE_VLM_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
            ),
            full_page_vlm_preset=(
                os.getenv("DOCS_PARSER_FULL_PAGE_VLM_PRESET", "qwen").strip() or "qwen"
            ),
            full_page_vlm_timeout_sec=max(
                10.0,
                _env_float("DOCS_PARSER_FULL_PAGE_VLM_TIMEOUT_SEC", 120.0),
            ),
            full_page_vlm_concurrency=max(
                1,
                _env_int("DOCS_PARSER_FULL_PAGE_VLM_CONCURRENCY", 6),
            ),
            full_page_vlm_batch_size=max(
                1,
                _env_int("DOCS_PARSER_FULL_PAGE_VLM_BATCH_SIZE", 2),
            ),
            full_page_vlm_max_new_tokens=max(
                64,
                _env_int("DOCS_PARSER_FULL_PAGE_VLM_MAX_NEW_TOKENS", 1200),
            ),
            full_page_vlm_scale=max(
                0.5,
                _env_float("DOCS_PARSER_FULL_PAGE_VLM_SCALE", 2.0),
            ),
            asset_reinspection_api_key=(
                os.getenv("DOCS_PARSER_ASSET_REINSPECTION_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            ).strip(),
            asset_reinspection_api_url=(
                os.getenv("DOCS_PARSER_ASSET_REINSPECTION_API_URL", "https://api.openai.com/v1/chat/completions").strip()
                or "https://api.openai.com/v1/chat/completions"
            ),
            asset_reinspection_model=(
                os.getenv("DOCS_PARSER_ASSET_REINSPECTION_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
            ),
            asset_reinspection_timeout_sec=max(
                10.0,
                _env_float("DOCS_PARSER_ASSET_REINSPECTION_TIMEOUT_SEC", 90.0),
            ),
            asset_reinspection_max_new_tokens=max(
                128,
                _env_int("DOCS_PARSER_ASSET_REINSPECTION_MAX_NEW_TOKENS", 900),
            ),
            asset_reinspection_detail=(
                os.getenv("DOCS_PARSER_ASSET_REINSPECTION_DETAIL", "high").strip().lower() or "high"
            ),
            asset_reinspection_prompt=_load_text(
                Path(
                    os.getenv(
                        "DOCS_PARSER_ASSET_REINSPECTION_PROMPT_PATH",
                        str(AGENT_ROOT / "prompts" / "asset_reinspection.md"),
                    )
                ),
                cls.asset_reinspection_prompt,
            ),
            image_heavy_low_text_chars_per_unit=max(
                1,
                _env_int("DOCS_PARSER_IMAGE_HEAVY_LOW_TEXT_CHARS_PER_UNIT", 160),
            ),
            image_heavy_low_text_unit_ratio_threshold=min(
                1.0,
                max(0.0, _env_float("DOCS_PARSER_IMAGE_HEAVY_LOW_TEXT_UNIT_RATIO_THRESHOLD", 0.55)),
            ),
            image_heavy_visual_unit_ratio_threshold=min(
                1.0,
                max(0.0, _env_float("DOCS_PARSER_IMAGE_HEAVY_VISUAL_UNIT_RATIO_THRESHOLD", 0.35)),
            ),
            image_heavy_chunk_density_threshold=min(
                1.0,
                max(0.0, _env_float("DOCS_PARSER_IMAGE_HEAVY_CHUNK_DENSITY_THRESHOLD", 0.35)),
            ),
            image_heavy_very_low_avg_text_chars_threshold=max(
                1,
                _env_int("DOCS_PARSER_IMAGE_HEAVY_VERY_LOW_AVG_TEXT_CHARS_THRESHOLD", 90),
            ),
            max_chunk_chars=max(800, _env_int("DOCS_PARSER_MAX_CHUNK_CHARS", 2400)),
            chunk_overlap_chars=max(0, _env_int("DOCS_PARSER_CHUNK_OVERLAP_CHARS", 280)),
        )
