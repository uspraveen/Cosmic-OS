"""Live deck-build progress snapshots for the Cosmic UI.

Workflows call ``emit_slide_progress`` as plan/design/render/QA complete.
The Slide Agent folds those events into one snapshot so the desktop can show
a plan card and real slide thumbnails while the pipeline is still running.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None] | None

_STAGE_ORDER = {
    "plan": 0,
    "design": 1,
    "render": 2,
    "qa": 3,
    "convert": 4,
    "ready": 5,
}


def compact_slide_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Drop full_content so the UI can stream a plan without megabyte payloads."""
    source = plan if isinstance(plan, dict) else {}
    slides: list[dict[str, Any]] = []
    for raw in source.get("slides") or []:
        if not isinstance(raw, dict):
            continue
        number = _as_int(raw.get("slide_number")) or (len(slides) + 1)
        title = str(raw.get("title") or "").strip()
        role = str(raw.get("content_role") or "").strip()
        slides.append(
            {
                "slide_number": number,
                "title": title,
                "content_role": role,
            }
        )
    return {
        "deck_title": str(source.get("deck_title") or "").strip(),
        "deck_theme": str(source.get("deck_theme") or "").strip(),
        "slides": slides,
    }


def emit_slide_progress(on_progress: ProgressCallback, **payload: Any) -> None:
    if on_progress is None:
        return
    body = {key: value for key, value in payload.items() if value is not None}
    if not body:
        return
    try:
        on_progress(body)
    except Exception:
        logger.debug("slide live progress callback failed", exc_info=True)


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class LiveDeckProgress:
    """Thread-safe accumulator for one slide.create / slide.edit run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stage = "plan"
        self.label = ""
        self.detail = ""
        self.plan: dict[str, Any] | None = None
        self.slides: dict[int, dict[str, Any]] = {}
        self.current = 0
        self.total = 0

    def apply(self, event: dict[str, Any] | None) -> dict[str, Any]:
        incoming = event if isinstance(event, dict) else {}
        with self._lock:
            stage = str(incoming.get("stage") or "").strip().lower()
            if stage in _STAGE_ORDER:
                self.stage = stage
            label = str(incoming.get("label") or "").strip()
            if label:
                self.label = label
            detail = str(incoming.get("detail") or "").strip()
            if detail:
                self.detail = detail
            plan = incoming.get("plan")
            if isinstance(plan, dict):
                self.plan = compact_slide_plan(plan)
                if not self.total:
                    self.total = len(self.plan.get("slides") or [])
            total = _as_int(incoming.get("total"))
            if total:
                self.total = total
            elif self.plan and not self.total:
                self.total = len(self.plan.get("slides") or [])
            slide_number = _as_int(incoming.get("slide_number"))
            if slide_number:
                self.current = slide_number
                current = dict(self.slides.get(slide_number) or {})
                current["slide_number"] = slide_number
                title = str(incoming.get("title") or current.get("title") or "").strip()
                if title:
                    current["title"] = title
                status = str(incoming.get("status") or "").strip()
                if status:
                    current["status"] = status
                elif "status" not in current:
                    current["status"] = stage or "pending"
                for key in (
                    "png_path",
                    "path",
                    "artifact_id",
                    "mime",
                    "filename",
                    "sha256",
                    "preview_url",
                ):
                    value = incoming.get(key)
                    if value not in (None, "", [], {}):
                        current[key] = value
                self.slides[slide_number] = current
            return self.snapshot_locked()

    def absorb_slides(self, slides: list[dict[str, Any]] | None) -> None:
        """Keep stamped preview artifacts on the tracker after files are copied."""
        if not isinstance(slides, list):
            return
        with self._lock:
            for item in slides:
                if not isinstance(item, dict):
                    continue
                number = _as_int(item.get("slide_number"))
                if not number:
                    continue
                current = dict(self.slides.get(number) or {})
                for key, value in item.items():
                    if value not in (None, "", [], {}):
                        current[key] = value
                current["slide_number"] = number
                self.slides[number] = current

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict[str, Any]:
        slides = [dict(item) for _, item in sorted(self.slides.items())]
        total = self.total or (len(self.plan.get("slides") or []) if self.plan else 0) or len(slides)
        current = self.current or len(slides)
        rendered = sum(1 for item in slides if item.get("path") or item.get("png_path"))
        if self.stage == "ready":
            percent = 1.0
        elif self.stage == "plan":
            percent = 0.08
        elif self.stage == "design":
            percent = 0.12 + 0.28 * (len(slides) / max(total, 1))
        elif self.stage in {"render", "qa"}:
            percent = 0.42 + 0.38 * (rendered / max(total, 1))
        elif self.stage == "convert":
            percent = 0.88
        else:
            percent = min(0.95, 0.2 + 0.6 * (rendered / max(total, 1)))
        return {
            "kind": "slide_build",
            "stage": self.stage,
            "label": self.label,
            "detail": self.detail or None,
            "current": current,
            "total": max(total, 1) if self.label else total,
            "percent": round(min(1.0, max(0.0, percent)), 3),
            "plan": dict(self.plan) if self.plan else None,
            "slides": slides,
        }
