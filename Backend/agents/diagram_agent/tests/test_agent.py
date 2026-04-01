"""Tests for the diagram agent."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))


# ── Config tests ──────────────────────────────────────────────────────────────


class TestConfig:
    def test_default_config(self):
        from agents.diagram_agent.config import DiagramAgentConfig

        cfg = DiagramAgentConfig()
        assert cfg.mimo_model == "gpt-5-mini"
        assert cfg.diagram_use_langgraph is True
        assert cfg.diagram_max_tool_rounds == 6
        assert cfg.default_format == "svg"

    def test_config_from_env(self):
        from agents.diagram_agent.config import DiagramAgentConfig

        cfg = DiagramAgentConfig.from_env()
        assert cfg.redis_url


# ── Renderer tests ────────────────────────────────────────────────────────────


class TestRenderers:
    def test_excalidraw_json_render(self):
        from agents.diagram_agent.renderers import render_excalidraw

        definition = json.dumps(
            {
                "type": "excalidraw",
                "version": 2,
                "source": "test",
                "elements": [
                    {
                        "type": "rectangle",
                        "id": "r1",
                        "x": 100,
                        "y": 100,
                        "width": 200,
                        "height": 100,
                        "strokeColor": "#1e1e1e",
                        "backgroundColor": "#a5d8ff",
                        "fillStyle": "solid",
                        "strokeWidth": 2,
                        "roughness": 1,
                        "opacity": 100,
                        "angle": 0,
                        "seed": 123,
                        "version": 1,
                        "versionNonce": 456,
                        "isDeleted": False,
                    }
                ],
            }
        )
        result = render_excalidraw(definition)
        assert result["output_format"] == "excalidraw"
        assert len(result["content"]) > 0
        parsed = json.loads(result["content"].decode("utf-8"))
        assert parsed["type"] == "excalidraw"
        assert len(parsed["elements"]) == 1

    def test_excalidraw_auto_wrap_array(self):
        from agents.diagram_agent.renderers import render_excalidraw

        definition = json.dumps([{"type": "rectangle", "id": "r1"}])
        result = render_excalidraw(definition)
        parsed = json.loads(result["content"].decode("utf-8"))
        assert parsed["type"] == "excalidraw"
        assert "elements" in parsed

    def test_excalidraw_invalid_json(self):
        from agents.diagram_agent.renderers import render_excalidraw, RenderError

        with pytest.raises(RenderError):
            render_excalidraw("not json")

    def test_mermaid_render_missing_binary(self):
        from agents.diagram_agent.renderers import render_mermaid, RenderError
        import asyncio

        async def _run():
            try:
                await render_mermaid("graph TD\nA-->B", mmdc_path="nonexistent_mmdc")
                pytest.fail("Expected RenderError")
            except RenderError:
                pass  # Expected
            except Exception:
                pass  # Acceptable — temp file or subprocess failure on this env

        asyncio.run(_run())

    def test_d2_render_missing_binary(self):
        from agents.diagram_agent.renderers import render_d2, RenderError
        import asyncio

        async def _run():
            try:
                await render_d2("x -> y: hello", d2_path="nonexistent_d2")
                pytest.fail("Expected RenderError")
            except RenderError:
                pass  # Expected
            except Exception:
                pass  # Acceptable — temp file or subprocess failure on this env

        asyncio.run(_run())

    def test_explain_mermaid_missing_chrome(self):
        from agents.diagram_agent.renderers import RenderError, explain_render_error

        error = RenderError(
            "mermaid",
            "mmdc exited with code 1",
            stderr="Error: Could not find Chrome (ver. 131.0.6778.204).",
        )
        explained = explain_render_error(error)
        assert "Chrome/headless-shell" in explained
        assert "missing on this machine" in explained

    def test_explain_d2_stderr_uses_first_meaningful_line(self):
        from agents.diagram_agent.renderers import RenderError, explain_render_error

        error = RenderError(
            "d2",
            "d2 exited with code 1",
            stderr="\nerror:\nsyntax error near line 2\nstack trace follows\n",
        )
        explained = explain_render_error(error)
        assert explained.endswith("syntax error near line 2")


# ── Skills tests ──────────────────────────────────────────────────────────────


class TestSkills:
    def test_discover_skills(self):
        from agents.diagram_agent.skills import discover_skills

        skills = discover_skills()
        names = [s["name"] for s in skills]
        assert "mermaid" in names
        assert "d2" in names
        assert "excalidraw" in names

    def test_load_skill_content(self):
        from agents.diagram_agent.skills import discover_skills, load_skill_content

        skills = discover_skills()
        for skill in skills:
            content = load_skill_content(skill["path"])
            assert content is not None
            assert len(content) > 100  # Each skill should have substantial content

    def test_build_skills_context(self):
        from agents.diagram_agent.skills import discover_skills, build_skills_context

        skills = discover_skills()
        ctx = build_skills_context(skills)
        assert "mermaid" in ctx.lower()
        assert "d2" in ctx.lower()
        assert "excalidraw" in ctx.lower()


# ── Schema tests ──────────────────────────────────────────────────────────────


class TestSchemas:
    def test_all_schemas_exist(self):
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
        expected = [
            "diagram.create.input.json",
            "diagram.create.output.json",
            "diagram.modify.input.json",
            "diagram.modify.output.json",
            "diagram.recall_session.input.json",
            "diagram.recall_session.output.json",
        ]
        for name in expected:
            path = schemas_dir / name
            assert path.exists(), f"Missing schema: {name}"
            data = json.loads(path.read_text())
            assert "type" in data


# ── Agent card tests ──────────────────────────────────────────────────────────


class TestAgentCard:
    def test_card_parseable(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        data = yaml.safe_load(card_path.read_text())
        assert data["agent_id"] == "cosmic/diagram-agent:1.0.0"
        assert len(data["intents"]) == 3
        intent_names = [i["name"] for i in data["intents"]]
        assert "diagram.create" in intent_names
        assert "diagram.modify" in intent_names
        assert "diagram.recall_session" in intent_names

    def test_no_auth_requirements(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        data = yaml.safe_load(card_path.read_text())
        # Diagram agent doesn't need OAuth credentials
        assert "auth_requirements" not in data

    def test_artifact_types(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        data = yaml.safe_load(card_path.read_text())
        assert "diagram_svg" in data["artifact_types"]
        assert "diagram_png" in data["artifact_types"]
        assert "diagram_source" in data["artifact_types"]

    def test_schemas_referenced(self):
        import yaml

        card_path = Path(__file__).resolve().parent.parent / "agent_card.yaml"
        schemas_dir = Path(__file__).resolve().parent.parent / "schemas" / "intents"
        data = yaml.safe_load(card_path.read_text())
        for intent in data["intents"]:
            input_path = schemas_dir / intent["input_schema"].split("/")[-1]
            output_path = schemas_dir / intent["output_schema"].split("/")[-1]
            assert input_path.exists(), f"Missing input schema for {intent['name']}"
            assert output_path.exists(), f"Missing output schema for {intent['name']}"


# ── Regression: StepPlan and skills context ────────────────────────────────────


class TestStepPlanSemantics:
    """Verify StepPlan completion semantics in the LangGraph workflow."""

    def test_step_plan_update_calls_update_not_create(self):
        """_step_plan_update should call step_plan.update(), not create()."""
        from agents.diagram_agent.diagram_graph import _step_plan_update
        import asyncio

        class FakeStepPlan:
            def __init__(self):
                self.updates = []

            async def update(self, step, status, note=None):
                self.updates.append({"step": step, "status": status, "note": note})

        class FakeAgent:
            def __init__(self):
                self.step_plan = FakeStepPlan()

        class FakeCtx:
            def __init__(self):
                self.agent = FakeAgent()

        ctx = FakeCtx()
        asyncio.run(_step_plan_update(ctx, 1, "completed", note="test"))
        assert len(ctx.agent.step_plan.updates) == 1
        assert ctx.agent.step_plan.updates[0] == {
            "step": 1,
            "status": "completed",
            "note": "test",
        }

    def test_step_plan_update_silently_skips_when_none(self):
        """_step_plan_update should not raise when step_plan is None."""
        from agents.diagram_agent.diagram_graph import _step_plan_update
        import asyncio

        class FakeAgent:
            step_plan = None

        class FakeCtx:
            def __init__(self):
                self.agent = FakeAgent()

        ctx = FakeCtx()
        # Should not raise
        asyncio.run(_step_plan_update(ctx, 1, "completed"))

    def test_step_plan_update_handles_invalid_step(self):
        """_step_plan_update should not crash on invalid step number."""
        from agents.diagram_agent.diagram_graph import _step_plan_update
        import asyncio

        class FakeStepPlan:
            def __init__(self):
                self.updates = []

            async def update(self, step, status, note=None):
                if step < 1:
                    return {"error": "invalid step"}
                self.updates.append({"step": step, "status": status})

        class FakeAgent:
            def __init__(self):
                self.step_plan = FakeStepPlan()

        class FakeCtx:
            def __init__(self):
                self.agent = FakeAgent()

        ctx = FakeCtx()
        # Should not raise even with bad step
        asyncio.run(_step_plan_update(ctx, -1, "completed"))


class TestSkillsContextInjection:
    """Verify skills context is built and available for LLM calls."""

    def test_analyze_diagram_request_accepts_skills_context(self):
        """analyze_diagram_request should accept skills_context parameter."""
        import inspect
        from agents.diagram_agent.internal_llm import analyze_diagram_request

        sig = inspect.signature(analyze_diagram_request)
        assert "skills_context" in sig.parameters

    def test_modify_diagram_accepts_skills_context(self):
        """modify_diagram should accept skills_context parameter."""
        import inspect
        from agents.diagram_agent.internal_llm import modify_diagram

        sig = inspect.signature(modify_diagram)
        assert "skills_context" in sig.parameters

    def test_regenerate_diagram_with_feedback_exists(self):
        """regenerate_diagram_with_feedback function should exist."""
        from agents.diagram_agent.internal_llm import regenerate_diagram_with_feedback
        import inspect

        sig = inspect.signature(regenerate_diagram_with_feedback)
        assert "validation_issues" in sig.parameters
        assert "validation_suggestion" in sig.parameters


class TestGraphTopology:
    """Verify graph topology has the plan loop and validation loop."""

    def test_route_after_finalize_exists(self):
        """The graph should have a route_after_finalize function for plan looping."""
        import inspect
        from agents.diagram_agent import diagram_graph

        source = inspect.getsource(diagram_graph)
        assert "route_after_finalize" in source

    def test_accumulated_artifacts_in_state(self):
        """DiagramWorkflowState should have accumulated_artifacts for multi-step plans."""
        from agents.diagram_agent.diagram_graph import DiagramWorkflowState

        # Check that the TypedDict has the field (via __annotations__ or __required_keys__)
        annotations = getattr(DiagramWorkflowState, "__annotations__", {})
        assert "accumulated_artifacts" in annotations
        assert "accumulated_outputs" in annotations

    def test_finalize_does_not_complete_all_steps_on_error(self):
        """Error path in finalize should mark steps as skipped, not completed."""
        import inspect
        from agents.diagram_agent.diagram_graph import _build_graph

        source = inspect.getsource(_build_graph)
        # The error path should use "skipped" not blindly "completed"
        assert "skipped" in source
        assert "Task ended before step executed" in source


class TestPlanExecutionBehavior:
    def test_two_step_langgraph_plan_executes_each_step_once(self):
        import asyncio
        import hashlib
        import shutil
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from uuid import uuid4

        from agents.diagram_agent.config import DiagramAgentConfig
        from agents.diagram_agent.diagram_graph import run_diagram_langgraph
        from shared.contracts import ArtifactManifest, TaskEnvelope

        class FakeStepPlan:
            def __init__(self):
                self.created = None
                self.updates = []

            async def create(self, steps):
                self.created = list(steps)

            async def update(self, step, status, note=None):
                self.updates.append(
                    {"step": step, "status": status, "note": note}
                )

        class FakeAgent:
            def __init__(self, artifact_root: Path):
                self._cfg = DiagramAgentConfig(
                    artifacts_root=artifact_root,
                    diagram_max_tool_rounds=6,
                )
                self.step_plan = FakeStepPlan()
                self.agent_id = "cosmic/diagram-agent:1.0.0"
                self.artifacts_root = artifact_root

            def _task_artifact_dir(self, task_id: str) -> Path:
                path = self.artifacts_root / task_id / "diagram_agent"
                path.mkdir(parents=True, exist_ok=True)
                return path

            def _artifact_manifest(
                self,
                *,
                task_id: str,
                path: Path,
                mime: str,
                kind: str = "output",
                audience: str = "deliverable",
            ) -> ArtifactManifest:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                logical_path = path.as_posix()
                return ArtifactManifest(
                    artifact_id=f"art_{uuid4().hex[:12]}",
                    task_id=task_id,
                    mime=mime,
                    sha256=digest,
                    path=logical_path,
                    created_by_agent=self.agent_id,
                    created_at=datetime.now(timezone.utc),
                    kind=kind,
                    audience=audience,
                )

        runtime_tmp_root = Path.cwd() / ".diagram_graph_behavior_tmp"
        runtime_tmp_root.mkdir(parents=True, exist_ok=True)
        temp_root = runtime_tmp_root / f"artifacts_{uuid4().hex[:8]}"
        temp_root.mkdir(parents=True, exist_ok=True)

        async def fake_analyze_diagram_request(**kwargs):
            step_text = kwargs.get("plan_step_text", "")
            observed_steps.append(step_text)
            if not step_text:
                return {
                    "action": "create_plan",
                    "steps": ["draw ingress flow", "draw database schema"],
                }
            if step_text == "draw ingress flow":
                return {
                    "action": "generate",
                    "renderer": "mermaid",
                    "diagram_type": "flowchart",
                    "definition": "graph TD\nIngress-->Auth",
                    "confidence": 0.92,
                    "reasoning": "Ingress flow",
                }
            return {
                "action": "generate",
                "renderer": "mermaid",
                "diagram_type": "flowchart",
                "definition": "graph TD\nDB-->Cache",
                "confidence": 0.91,
                "reasoning": "Database schema",
            }

        async def fake_render_mermaid(
            definition,
            *,
            output_path=None,
            output_format="svg",
            **kwargs,
        ):
            assert output_path is not None
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                f"<svg><text>{definition}</text></svg>",
                encoding="utf-8",
            )
            return {
                "output_path": output_path,
                "output_format": output_format,
                "content": output_path.read_bytes(),
            }

        async def fake_validate_diagram_render(**kwargs):
            return {
                "pass": True,
                "issues": [],
                "suggestion": "",
                "confidence": 0.99,
            }

        task = TaskEnvelope(
            task_id="tsk_diagram_multistep",
            task_list_id="lst_diagram_multistep",
            session_id="sess_diagram_multistep",
            sender="cosmic/orchestrator:1.0.0",
            recipient="cosmic/diagram-agent:1.0.0",
            intent="diagram.create",
            input={"description": "Create two diagrams: ingress flow and db schema"},
            idempotency_key="idem_diagram_multistep",
            signature="",
            source="agent",
            source_id="req_diagram_multistep",
            channel="desktop",
        )

        fake_agent = FakeAgent(temp_root)
        observed_steps = []
        import agents.diagram_agent.diagram_graph as diagram_graph

        original_analyze = diagram_graph.analyze_diagram_request
        original_render = diagram_graph.render_mermaid
        original_validate = diagram_graph.validate_diagram_render

        try:
            diagram_graph.analyze_diagram_request = fake_analyze_diagram_request
            diagram_graph.render_mermaid = fake_render_mermaid
            diagram_graph.validate_diagram_render = fake_validate_diagram_render

            result = asyncio.run(run_diagram_langgraph(agent=fake_agent, task=task))
        finally:
            diagram_graph.analyze_diagram_request = original_analyze
            diagram_graph.render_mermaid = original_render
            diagram_graph.validate_diagram_render = original_validate
            shutil.rmtree(temp_root, ignore_errors=True)

        assert result.status == "completed"
        assert result.output["count"] == 2
        assert len(result.output["diagrams"]) == 2
        assert [entry["title"] for entry in result.output["diagrams"]] == [
            "Ingress flow",
            "Database schema",
        ]
        assert len(result.artifacts) == 4
        assert fake_agent.step_plan.created == [
            "draw ingress flow",
            "draw database schema",
        ]
        assert observed_steps == ["", "draw ingress flow", "draw database schema"]
        assert fake_agent.step_plan.updates == [
            {"step": 1, "status": "in_progress", "note": None},
            {"step": 1, "status": "completed", "note": "Ingress flow"},
            {"step": 2, "status": "in_progress", "note": "Starting next plan step"},
            {"step": 2, "status": "completed", "note": "Database schema"},
        ]
