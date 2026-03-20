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


@dataclass(slots=True)
class DocsParserConfig:
    redis_url: str = "redis://127.0.0.1:6379/0"
    gateway_url: str = "http://127.0.0.1:8080"
    gateway_internal_token: str = ""
    max_input_artifacts: int = 8
    max_input_file_bytes: int = 20 * 1024 * 1024
    max_num_pages: int = 200
    default_enable_ocr: bool = True
    default_generate_page_images: bool = False
    default_generate_picture_images: bool = True
    max_chunk_chars: int = 2400
    chunk_overlap_chars: int = 280

    @classmethod
    def from_env(cls) -> "DocsParserConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip() or "redis://127.0.0.1:6379/0",
            gateway_url=os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").strip() or "http://127.0.0.1:8080",
            gateway_internal_token=os.getenv("GATEWAY_INTERNAL_TOKEN", "").strip(),
            max_input_artifacts=max(1, _env_int("DOCS_PARSER_MAX_INPUT_ARTIFACTS", 8)),
            max_input_file_bytes=max(1024 * 1024, _env_int("DOCS_PARSER_MAX_INPUT_FILE_BYTES", 20 * 1024 * 1024)),
            max_num_pages=max(1, _env_int("DOCS_PARSER_MAX_NUM_PAGES", 200)),
            default_enable_ocr=os.getenv("DOCS_PARSER_ENABLE_OCR", "true").strip().lower() not in {"0", "false", "no"},
            default_generate_page_images=os.getenv("DOCS_PARSER_GENERATE_PAGE_IMAGES", "false").strip().lower()
            in {"1", "true", "yes"},
            default_generate_picture_images=os.getenv("DOCS_PARSER_GENERATE_PICTURE_IMAGES", "true").strip().lower()
            not in {"0", "false", "no"},
            max_chunk_chars=max(800, _env_int("DOCS_PARSER_MAX_CHUNK_CHARS", 2400)),
            chunk_overlap_chars=max(0, _env_int("DOCS_PARSER_CHUNK_OVERLAP_CHARS", 280)),
        )
