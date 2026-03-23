"""Single source of truth for tabular specialist guidance (staged internal context).

Deep spreadsheet / FP&A guidance lives here—not in the orchestrator system prompt.
Use :func:`build_internal_context` to compose minimal, stage-specific strings for
internal MiMo / future planner / executor hooks.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

TabularInternalStage = Literal["summarize", "plan", "execute"]

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_STAGED_FILE = "tabular_staged_context.md"

# FP&A supplement is intentionally not attached to execute (keeps executor prompt small).
_FPNA_STAGES: Final[frozenset[str]] = frozenset({"summarize", "plan"})

_HEADER_RE = re.compile(r"^##\s+(\S+)\s*$", re.MULTILINE)


@lru_cache(maxsize=1)
def _load_staged_sections() -> dict[str, str]:
    path = _PROMPT_DIR / _STAGED_FILE
    text = path.read_text(encoding="utf-8")
    chunks = _HEADER_RE.split(text)
    sections: dict[str, str] = {}
    # chunks[0] is preamble before first ##; then (name, body) pairs.
    for i in range(1, len(chunks), 2):
        name = chunks[i].strip().lower()
        body = chunks[i + 1] if i + 1 < len(chunks) else ""
        sections[name] = body.strip()
    return sections


def build_internal_context(stage: TabularInternalStage | str, *, include_fpna: bool = False) -> str:
    """Compose minimal internal context for a tabular specialist stage.

    Stages are composition-only hooks (``summarize`` is used by MiMo today;
    ``plan`` / ``execute`` are reserved for future internal steps—no new product
    runtime stages are implied).

    Args:
        stage: ``summarize`` | ``plan`` | ``execute``
        include_fpna: When True, appends the ``fpna_supplement`` block for
            ``summarize`` and ``plan`` only (never for ``execute``).

    Returns:
        Non-empty string: ``shared`` + stage body + optional FP&A supplement,
        joined with blank lines. Each block appears at most once.
    """
    stage_key = str(stage).strip().lower()
    if stage_key not in ("summarize", "plan", "execute"):
        raise ValueError(f"Unknown tabular internal stage: {stage!r}")

    sections = _load_staged_sections()
    blocks: list[str] = []
    shared = sections.get("shared", "").strip()
    if shared:
        blocks.append(shared)
    stage_body = sections.get(stage_key, "").strip()
    if stage_body:
        blocks.append(stage_body)
    if include_fpna and stage_key in _FPNA_STAGES:
        fpna = sections.get("fpna_supplement", "").strip()
        if fpna:
            blocks.append(fpna)
    return "\n\n".join(blocks)


__all__ = [
    "TabularInternalStage",
    "build_internal_context",
]
