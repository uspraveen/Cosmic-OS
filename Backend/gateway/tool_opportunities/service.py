from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .store import ToolOpportunityStore


DEFAULT_OPPORTUNITIES: tuple[dict[str, Any], ...] = (
    {
        "seed_key": "personal-portfolio-site",
        "title": "Personal Portfolio Site",
        "tool_type": "site",
        "goal": "Present the user's work, experience, and strongest projects in a polished site that can evolve with their career.",
        "reasoning": "A persistent portfolio gives applications, introductions, and collaborators one high-quality place to understand the user's work.",
        "proposed_features": ["Project showcase", "Experience and skills", "Contact and social links", "Easy content updates"],
        "helpful_materials": ["Resume or CV", "Project links or screenshots", "Short bio", "Headshot", "Preferred visual style"],
        "required_inputs": [],
        "expected_value": "A reusable public presence for applications, networking, and credibility.",
    },
    {
        "seed_key": "job-search-command-center",
        "title": "Job Search Command Center",
        "tool_type": "tracker",
        "goal": "Track roles, applications, contacts, follow-ups, interviews, and next actions in one focused workspace.",
        "reasoning": "A custom workflow can be more useful than a generic spreadsheet when applications, people, reminders, and preparation need to stay connected.",
        "proposed_features": ["Application pipeline", "Follow-up reminders", "Company and contact notes", "Interview preparation"],
        "helpful_materials": ["Resume", "Target roles", "Target companies", "Existing application tracker"],
        "required_inputs": [],
        "expected_value": "Less application drift and clearer next actions.",
    },
    {
        "seed_key": "personal-relationship-crm",
        "title": "Personal Relationship CRM",
        "tool_type": "tracker",
        "goal": "Keep important relationships, introductions, promises, and follow-ups organized without losing context.",
        "reasoning": "High-value relationships often span email, meetings, and projects; a tailored view can make follow-through reliable.",
        "proposed_features": ["People and organizations", "Conversation context", "Follow-up queue", "Warm introduction tracking"],
        "helpful_materials": ["Existing contact list", "Relationship categories", "Important open conversations"],
        "required_inputs": [],
        "expected_value": "More consistent follow-through across important relationships.",
    },
    {
        "seed_key": "project-command-center",
        "title": "Project Command Center",
        "tool_type": "dashboard",
        "goal": "Create a focused operating view for a project, including priorities, progress, decisions, risks, and links.",
        "reasoning": "Complex projects benefit from a purpose-built view that reflects how the user actually works instead of a generic task list.",
        "proposed_features": ["Milestones", "Current priorities", "Open decisions", "Project links and artifacts"],
        "helpful_materials": ["Project notes", "Existing roadmap", "Relevant links", "Preferred workflow"],
        "required_inputs": [],
        "expected_value": "Faster project orientation and less context loss.",
    },
    {
        "seed_key": "research-radar",
        "title": "Research Radar",
        "tool_type": "dashboard",
        "goal": "Maintain a living view of topics, papers, companies, people, and developments the user wants to follow.",
        "reasoning": "A persistent research surface turns repeated exploration into a compounding knowledge system.",
        "proposed_features": ["Topic watchlists", "Paper and link library", "Notes and comparisons", "New-development queue"],
        "helpful_materials": ["Topics of interest", "Existing reading list", "Preferred sources"],
        "required_inputs": [],
        "expected_value": "A continuously useful research workspace rather than scattered notes.",
    },
)


class ToolOpportunityService:
    def __init__(self, *, store: ToolOpportunityStore, export_path: Path) -> None:
        self.store = store
        self.export_path = export_path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.store.initialize()
        await self._seed_defaults()
        await self._sync_export()

    def summary(self) -> dict[str, Any]:
        return {**self.store.summary(), "export_path": str(self.export_path)}

    def list_items(self, *, statuses: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_items(statuses=statuses, limit=limit)

    def get_item(self, opportunity_id: str) -> dict[str, Any] | None:
        return self.store.get_item(opportunity_id)

    async def capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = self._text(payload.get("title"))
        goal = self._text(payload.get("goal"))
        reasoning = self._text(payload.get("reasoning"))
        if not title or not goal or not reasoning:
            raise ValueError("title, goal, and reasoning are required")
        now = self._utcnow()
        item = {
            "opportunity_id": f"tool_{uuid4().hex[:12]}",
            "seed_key": None,
            "title": title,
            "tool_type": self._type(payload.get("tool_type")),
            "goal": goal,
            "reasoning": reasoning,
            "proposed_features": self._strings(payload.get("proposed_features"), 12),
            "helpful_materials": self._strings(payload.get("helpful_materials"), 12),
            "required_inputs": self._strings(payload.get("required_inputs"), 8),
            "data_sources": self._strings(payload.get("data_sources"), 12),
            "trigger_source": self._text(payload.get("trigger_source")) or "orchestrator",
            "source_context_refs": self._strings(payload.get("source_context_refs"), 12),
            "status": "suggested",
            "confidence": self._number(payload.get("confidence")),
            "expected_value": self._text(payload.get("expected_value")),
            "suggested_at": now,
            "last_presented_at": None,
            "presentation_count": 0,
            "user_feedback": None,
            "declined_reason": None,
            "defer_until": None,
            "alpha_project_id": None,
            "build_task_id": None,
            "deployment_url": None,
            "repo_url": None,
            "health_status": None,
            "last_checked_at": None,
            "created_by": self._text(payload.get("created_by")) or "cosmic/orchestrator:1.0.0",
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "created_at": now,
            "updated_at": now,
        }
        async with self._lock:
            created = self.store.create(item)
            await self._sync_export()
        return created

    async def update(self, opportunity_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        existing = self.store.get_item(opportunity_id)
        if existing is None:
            return None
        allowed_statuses = {"candidate", "suggested", "accepted", "building", "live", "declined", "deferred", "archived", "failed"}
        changes: dict[str, Any] = {"updated_at": self._utcnow()}
        status = self._text(payload.get("status"))
        if status:
            if status not in allowed_statuses:
                raise ValueError("unsupported status")
            changes["status"] = status
        for key in ("user_feedback", "declined_reason", "defer_until", "alpha_project_id", "build_task_id", "deployment_url", "repo_url", "health_status"):
            if key in payload:
                changes[key] = self._text(payload.get(key)) or None
        for key in ("title", "goal", "reasoning", "expected_value"):
            if key in payload and self._text(payload.get(key)):
                changes[key] = self._text(payload.get(key))
        for key in ("proposed_features", "helpful_materials", "required_inputs", "data_sources", "source_context_refs"):
            if key in payload:
                changes[f"{key}_json"] = json.dumps(self._strings(payload.get(key), 20), ensure_ascii=False, separators=(",", ":"))
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            changes["metadata_json"] = json.dumps({**existing.get("metadata", {}), **metadata}, ensure_ascii=False, separators=(",", ":"), default=str)
        if status == "accepted":
            changes["last_presented_at"] = self._utcnow()
            changes["presentation_count"] = int(existing.get("presentation_count") or 0) + 1
        async with self._lock:
            updated = self.store.update(opportunity_id, changes)
            await self._sync_export()
        return updated

    async def build_handoff(self, opportunity_id: str) -> dict[str, Any] | None:
        item = await self.update(opportunity_id, {"status": "accepted", "metadata": {"accepted_via": "my_tools"}})
        if item is None:
            return None
        helpful = item.get("helpful_materials") or []
        required = item.get("required_inputs") or []
        prompt = (
            f"Let's build the custom tool suggestion \"{item['title']}\" (tool opportunity {item['opportunity_id']}). "
            f"Goal: {item['goal']} "
            "Use Alpha when implementation should begin. First reason about the best product shape and ask for any genuinely useful additional materials "
            "or preferences before building, but do not treat optional materials as hard requirements. "
            f"Helpful optional materials: {', '.join(helpful) if helpful else 'none identified'}. "
            f"Required inputs: {', '.join(required) if required else 'none currently identified'}. "
            f"When delegating to alpha.execute, include constraints.tool_opportunity_id=\"{item['opportunity_id']}\". "
            "Keep this opportunity linked to its Alpha project and deployment when built."
        )
        return {"opportunity": item, "prompt": prompt}

    def heartbeat_digest(self, *, limit: int = 8) -> dict[str, Any]:
        items = self.store.list_items(statuses=["suggested", "accepted", "building", "live", "deferred"], limit=limit)
        return {
            "instruction": "Use this to avoid repeating declined suggestions and to notice when a persistent custom tool would materially advance an active goal. Offer first; do not autonomously build or deploy.",
            "items": [
                {
                    "opportunity_id": item.get("opportunity_id"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "goal": item.get("goal"),
                    "expected_value": item.get("expected_value"),
                    "alpha_project_id": item.get("alpha_project_id"),
                    "deployment_url": item.get("deployment_url"),
                }
                for item in items
            ],
        }

    async def _seed_defaults(self) -> None:
        now = self._utcnow()
        for seed in DEFAULT_OPPORTUNITIES:
            if self.store.get_by_seed_key(str(seed["seed_key"])):
                continue
            item = {
                **seed,
                "opportunity_id": f"tool_{uuid4().hex[:12]}",
                "data_sources": [],
                "trigger_source": "starter_library",
                "source_context_refs": [],
                "status": "suggested",
                "confidence": 0.75,
                "suggested_at": now,
                "last_presented_at": None,
                "presentation_count": 0,
                "user_feedback": None,
                "declined_reason": None,
                "defer_until": None,
                "alpha_project_id": None,
                "build_task_id": None,
                "deployment_url": None,
                "repo_url": None,
                "health_status": None,
                "last_checked_at": None,
                "created_by": "cosmic/starter-library:1.0.0",
                "metadata": {"starter": True},
                "created_at": now,
                "updated_at": now,
            }
            self.store.create(item)

    async def _sync_export(self) -> None:
        self.export_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": self._utcnow(), "items": self.store.list_items(limit=1000)}
        self.export_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    @staticmethod
    def _text(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @classmethod
    def _strings(cls, value: Any, limit: int) -> list[str]:
        raw = value if isinstance(value, list) else [value] if isinstance(value, str) else []
        out: list[str] = []
        for item in raw:
            text = cls._text(item)
            if text and text not in out:
                out.append(text)
            if len(out) >= limit:
                break
        return out

    @classmethod
    def _type(cls, value: Any) -> str:
        normalized = cls._text(value).lower()
        return normalized if normalized in {"site", "dashboard", "tracker", "portal", "workspace", "utility"} else "site"

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _utcnow() -> str:
        return datetime.now(timezone.utc).isoformat()
