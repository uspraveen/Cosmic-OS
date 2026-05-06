from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class StepPlan:
    """Flat per-task step tracker injected into agent runtimes."""

    def __init__(
        self,
        *,
        task_id: str,
        emit_event_fn: Callable[[str, str, dict[str, Any]], Awaitable[str]],
    ) -> None:
        self.task_id = task_id
        self._emit_event_fn = emit_event_fn
        self._steps: list[dict[str, Any]] = []
        self._active = False

    async def create(self, steps: list[str]) -> dict[str, Any]:
        cleaned = [str(step or "").strip() for step in steps if str(step or "").strip()]
        self._steps = [
            {"step": index, "text": text, "status": "pending", "note": None}
            for index, text in enumerate(cleaned, start=1)
        ]
        self._active = bool(self._steps)
        await self._emit_event_fn(
            self.task_id,
            "task.progress",
            {
                "type": "agent_plan_created",
                "total_steps": len(self._steps),
                "steps": [{"step": item["step"], "text": item["text"]} for item in self._steps],
            },
        )
        return {
            "plan_active": self._active,
            "total_steps": len(self._steps),
            "steps": self._copy_steps(),
        }

    async def update(self, step: int, status: str, note: str | None = None) -> dict[str, Any]:
        if not self._active:
            return {"error": "No active plan. Call create() first."}
        if step < 1 or step > len(self._steps):
            return {"error": f"Invalid step {step}. Valid: 1-{len(self._steps)}"}

        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"pending", "in_progress", "completed", "failed", "skipped"}:
            return {"error": f"Invalid status: {status!r}"}

        entry = self._steps[step - 1]
        entry["status"] = normalized_status
        if note is not None:
            entry["note"] = str(note)

        completed = sum(1 for item in self._steps if item["status"] in {"completed", "failed", "skipped"})
        total = len(self._steps)
        percent = round((completed / total) * 100) if total else 0

        await self._emit_event_fn(
            self.task_id,
            "task.progress",
            {
                "type": "agent_step_update",
                "step": step,
                "text": entry["text"],
                "status": normalized_status,
                "note": entry["note"],
                "completed": completed,
                "total": total,
                "percent": percent,
            },
        )
        return {
            "step": step,
            "status": normalized_status,
            "completed": completed,
            "total": total,
            "percent": percent,
        }

    async def list(self) -> dict[str, Any]:
        completed = sum(1 for item in self._steps if item["status"] in {"completed", "failed", "skipped"})
        return {
            "plan_active": self._active,
            "steps": self._copy_steps(),
            "completed": completed,
            "total": len(self._steps),
        }

    def has_pending_steps(self) -> bool:
        if not self._active:
            return False
        return any(item["status"] in {"pending", "in_progress"} for item in self._steps)

    def _copy_steps(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._steps]
