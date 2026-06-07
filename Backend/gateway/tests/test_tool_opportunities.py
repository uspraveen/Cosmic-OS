from __future__ import annotations

import asyncio
from pathlib import Path

from gateway.tool_opportunities import ToolOpportunityService, ToolOpportunityStore


def test_tool_opportunities_seed_defaults_and_build_handoff(tmp_path: Path) -> None:
    async def run() -> None:
        service = ToolOpportunityService(
            store=ToolOpportunityStore(tmp_path / "tools.db"),
            export_path=tmp_path / "exports" / "tools.json",
        )
        await service.initialize()

        items = service.list_items()
        portfolio = next(item for item in items if item.get("seed_key") == "personal-portfolio-site")
        assert portfolio["status"] == "suggested"
        assert "Resume or CV" in portfolio["helpful_materials"]
        assert portfolio["required_inputs"] == []

        handoff = await service.build_handoff(portfolio["opportunity_id"])
        assert handoff is not None
        assert handoff["opportunity"]["status"] == "accepted"
        assert "constraints.tool_opportunity_id" in handoff["prompt"]
        assert "do not treat optional materials as hard requirements" in handoff["prompt"]

        await service.initialize()
        assert len(service.list_items()) == len(items)
        assert (tmp_path / "exports" / "tools.json").exists()

    asyncio.run(run())


def test_tool_opportunity_lifecycle_links_alpha_project(tmp_path: Path) -> None:
    async def run() -> None:
        service = ToolOpportunityService(
            store=ToolOpportunityStore(tmp_path / "tools.db"),
            export_path=tmp_path / "tools.json",
        )
        await service.initialize()
        item = await service.capture(
            {
                "title": "Interview prep cockpit",
                "tool_type": "dashboard",
                "goal": "Keep interview research and preparation in one place.",
                "reasoning": "The workflow repeats across several active interviews.",
                "helpful_materials": ["Resume", "Job descriptions"],
            }
        )
        updated = await service.update(
            item["opportunity_id"],
            {
                "status": "live",
                "alpha_project_id": "alpha_proj_123",
                "deployment_url": "https://example.test",
            },
        )
        assert updated is not None
        assert updated["status"] == "live"
        assert updated["alpha_project_id"] == "alpha_proj_123"
        assert updated["deployment_url"] == "https://example.test"

    asyncio.run(run())


def test_weekly_review_refines_unaccepted_but_protects_user_owned_tools(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        store = ToolOpportunityStore(tmp_path / "tools.db")
        service = ToolOpportunityService(
            store=store,
            export_path=tmp_path / "tools.json",
        )
        await service.initialize()
        item = await service.capture(
            {
                "title": "General tracker",
                "tool_type": "tracker",
                "goal": "Track an active goal.",
                "reasoning": "The workflow repeats.",
            }
        )
        review_context = {
            "actor": "cosmic/orchestrator:1.0.0",
            "source": "cron",
            "source_id": "system.weekly_my_tools_review",
        }

        refined = await service.update(
            item["opportunity_id"],
            {
                "title": "Focused goal tracker",
                "reasoning": "Current projects show a focused tracker would now be useful.",
                "review_reason": "Refined against the user's active project context.",
                "mutation_context": review_context,
            },
        )
        assert refined is not None
        assert refined["title"] == "Focused goal tracker"

        accepted = await service.update(
            item["opportunity_id"],
            {"status": "accepted"},
        )
        assert accepted is not None
        protected = await service.update(
            item["opportunity_id"],
            {
                "title": "Silently rewritten title",
                "status": "archived",
                "health_status": "healthy",
                "review_reason": "Checked the accepted tool during weekly review.",
                "mutation_context": review_context,
            },
        )
        assert protected is not None
        assert protected["title"] == "Focused goal tracker"
        assert protected["status"] == "accepted"
        assert protected["health_status"] == "healthy"
        assert protected["review_guardrail"]["blocked_fields"] == ["status", "title"]

        events = store.list_events(opportunity_id=item["opportunity_id"])
        assert any(event["event_type"] == "weekly_review_updated" for event in events)
        assert any(event["event_type"] == "captured" for event in events)

    asyncio.run(run())
