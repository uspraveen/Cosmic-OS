"""Diagram-agent internal LLM via LangChain OpenAI-compatible client.

Uses gpt-5-mini to analyze diagram requests and generate diagram definitions.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import uuid4

import httpx

from shared.usage import UsageEvent, post_usage_event, serialize_usage_metadata

from .config import DiagramAgentConfig

logger = logging.getLogger(__name__)

_ANALYZE_SYSTEM = """\
You are a diagram analysis assistant. Given a natural language description of a diagram,
determine which renderer to use and extract a structured request.

Output ONLY valid JSON:
{
  "action": "create_plan" | "generate",
  "steps": ["step 1", "step 2", ...] — only for "create_plan": ordered list of concrete steps (3-8 steps)
  "renderer": "mermaid" | "d2" | "excalidraw" — only for "generate"
  "diagram_type": "flowchart" | "sequence" | "er" | "gantt" | "class" | "state" | "architecture" | "schema" | "network" | "whiteboard" | "other" — only for "generate"
  "title": "descriptive title for the diagram" — only for "generate"
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}

## When to use create_plan

Use **"create_plan"** as your FIRST action when the request involves 3 or more logical steps:
- Multiple diagrams in one request ("show me the architecture AND the auth flow")
- Diagram that needs exploration then generation ("analyze this codebase and diagram it")
- Modification + verification cycles
- Complex multi-part diagrams

Skip planning when:
- Single simple diagram request
- Straightforward modification

Plan steps must be concrete:
- "Generate Mermaid sequence diagram for auth flow"
- "Render to SVG"
- "Validate readability"

After creating a plan, the system will re-call you to execute step 1.

## When to use generate

Use **"generate"** for straightforward single-diagram requests. Provide:
- renderer, diagram_type, title, confidence, reasoning
- DO NOT generate the final diagram source code in this phase.
- Your job here is renderer selection and request normalization only.

## Renderer selection rules
- **Mermaid**: sequence diagrams, flowcharts, ER diagrams, Gantt charts, state diagrams, class diagrams, git graphs, pie charts, quadrant charts, timeline diagrams. Best for inline markdown/GitHub-compatible diagrams.
- **D2**: architecture diagrams, system schemas, network topologies, database schemas, API flows with nested containers. Best for structured, layered diagrams with clear hierarchy. Supports dark mode, sketch style.
- **Excalidraw**: hand-drawn whiteboard style, brainstorming, early-stage sketches, informal diagrams, wireframes. Best when user wants a casual/whiteboard feel.

## D2 syntax guardrails
- D2 direction values are words: `right`, `left`, `down`, `up`.
- NEVER use Graphviz/Mermaid direction aliases like `LR`, `RL`, `TB`, `TD`, or `BT` in D2 output.
- Keep D2 definitions idiomatic D2, not Mermaid or Graphviz syntax.

Diagram type mapping:
- "how X talks to Y", "flow", "process", "steps" → flowchart or sequence
- "API call", "request/response", "sequence of events" → sequence
- "database", "table", "entity", "relationship" → er
- "timeline", "schedule", "project plan" → gantt
- "class", "inheritance", "object" → class
- "state machine", "status", "workflow" → state
- "architecture", "system design", "infrastructure", "microservice" → architecture or d2
- "sketch", "whiteboard", "draw", "rough" → excalidraw
- "network", "topology", "VPC", "subnet" → d2
"""

_GENERATE_SYSTEM = """\
You are a diagram generation assistant. You have already been given the correct renderer.
Generate the final diagram source code using ONLY that renderer's syntax.

Output ONLY valid JSON:
{
  "renderer": "mermaid" | "d2" | "excalidraw",
  "diagram_type": "flowchart" | "sequence" | "er" | "gantt" | "class" | "state" | "architecture" | "schema" | "network" | "whiteboard" | "other",
  "title": "descriptive title for the diagram",
  "definition": "the final diagram source code",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation of the chosen structure"
}

Rules:
- Use ONLY the selected renderer's syntax and conventions.
- Do not mix syntax from Mermaid, D2, Excalidraw, Graphviz, or PlantUML.
- The selected renderer skill is provided in the context below. Follow it closely.
- Generate complete, valid source with no placeholders.
"""

_MODIFY_SYSTEM = """\
You are a diagram modification assistant. Given an existing diagram definition and a
modification request, produce the updated definition.

Output ONLY valid JSON:
{
  "renderer": "mermaid" | "d2" | "excalidraw",
  "definition": "updated diagram source code",
  "changes": "brief description of what was changed",
  "confidence": 0.0-1.0
}

Rules:
- Preserve the existing structure as much as possible
- Only change what was requested
- Maintain valid syntax for the renderer
- If the request is ambiguous, keep the original and note the ambiguity
"""


_REGENERATE_SYSTEM = """\
You are a diagram quality fixer. An existing diagram definition failed visual validation.
Regenerate a corrected version based on the validation feedback.

Output ONLY valid JSON:
{
  "renderer": "mermaid" | "d2" | "excalidraw",
  "diagram_type": "same as original",
  "title": "same as original",
  "definition": "corrected diagram source code",
  "confidence": 0.0-1.0,
  "reasoning": "what was fixed"
}

Rules:
- Keep the SAME renderer as the original
- Fix ONLY the issues mentioned in the validation feedback
- Maintain complete valid syntax — no placeholders
- Keep the same semantic content (nodes, connections, labels)
- Common fixes: reduce node count, simplify labels, change layout direction, add padding
"""


async def invoke_diagram_internal_llm(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    system_content: str,
    user_message: str,
    task_id: str | None,
    session_id: str | None,
    source: str | None,
    source_id: str | None,
    channel: str | None,
) -> dict[str, Any] | None:
    """Call gpt-5-mini for diagram analysis/generation. Returns parsed JSON or None."""
    if not cfg.enable_internal_llm or not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("diagram_agent.langchain_unavailable: %s", exc)
        return None

    messages = [
        SystemMessage(content=system_content.strip()),
        HumanMessage(content=user_message[:80_000]),
    ]

    started = time.perf_counter()
    llm_call_id = f"diagram_internal_llm_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.internal_llm_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as llm_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.internal_llm_model,
                "api_key": cfg.internal_llm_api_key,
                "base_url": cfg.internal_llm_base_url,
                "http_async_client": llm_http,
            }
            llm = ChatOpenAI(**llm_kwargs)
            result = await llm.ainvoke(messages)
    except Exception as exc:
        logger.warning("diagram_agent.internal_llm_error: %s", exc)
        return None

    latency_ms = (time.perf_counter() - started) * 1000
    raw_content = (result.content or "").strip() if result else ""

    usage_meta = {}
    if result:
        usage_meta = serialize_usage_metadata(result)

    try:
        await post_usage_event(
            http_client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=UsageEvent(
                llm_call_id=llm_call_id,
                llm_call_placed_at=time.time() - (latency_ms / 1000),
                agent_id="cosmic/diagram-agent:1.0.0",
                task_id=task_id or "",
                session_id=session_id or "",
                source=source or "",
                source_id=source_id or "",
                channel=channel or "",
                provider="openai_compatible",
                model=cfg.internal_llm_model,
                usage_kind="chat_completion",
                ok=True,
                latency_ms=latency_ms,
                usage=usage_meta,
            ),
        )
    except Exception:
        pass

    if not raw_content:
        return None

    # Parse JSON from response (handle markdown code blocks)
    json_str = raw_content
    if "```" in json_str:
        parts = json_str.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                json_str = stripped
                break

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return None


async def analyze_diagram_request(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    description: str,
    preferred_renderer: str | None = None,
    existing_definition: str | None = None,
    skills_index: str = "",
    plan_step_text: str = "",
    allow_planning: bool = True,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Analyze a diagram request and normalize it to renderer + plan metadata."""
    context = f"Description: {description}\n"
    if preferred_renderer:
        context += f"Preferred renderer: {preferred_renderer}\n"
    if existing_definition:
        context += (
            f"Existing definition to build upon:\n```\n{existing_definition}\n```\n"
        )
    if plan_step_text:
        context += f"\n---\nEXECUTING PLAN STEP: {plan_step_text}\nGenerate the diagram for this specific step. Use action: generate.\n"
    if not allow_planning:
        context += (
            "\n---\nPlanning is disabled for this call. Return action: generate.\n"
        )
    if skills_index:
        context += f"\n---\n{skills_index}\n"

    return await invoke_diagram_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_ANALYZE_SYSTEM,
        user_message=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


async def generate_diagram_definition(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    description: str,
    renderer: str,
    diagram_type: str,
    title: str,
    renderer_skill_context: str = "",
    plan_step_text: str = "",
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Generate the final diagram definition using the selected renderer skill only."""
    context = (
        f"Description: {description}\n"
        f"Selected renderer: {renderer}\n"
        f"Diagram type: {diagram_type}\n"
        f"Title: {title}\n"
    )
    if plan_step_text:
        context += f"Plan step focus: {plan_step_text}\n"
    if renderer_skill_context:
        context += f"\n---\n{renderer_skill_context}\n"

    return await invoke_diagram_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_GENERATE_SYSTEM,
        user_message=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


async def modify_diagram(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    renderer: str,
    existing_definition: str,
    modification_request: str,
    renderer_skill_context: str = "",
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Modify an existing diagram definition."""
    context = (
        f"Renderer: {renderer}\n"
        f"Existing definition:\n```\n{existing_definition}\n```\n"
        f"Modification request: {modification_request}\n"
    )
    if renderer_skill_context:
        context += f"\n---\n{renderer_skill_context}\n"

    return await invoke_diagram_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_MODIFY_SYSTEM,
        user_message=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )


_VALIDATE_SYSTEM = """\
You are a diagram quality reviewer. Examine the rendered diagram image and assess its readability.

Output ONLY valid JSON:
{
  "pass": true | false,
  "issues": ["list of specific issues found"],
  "suggestion": "brief actionable suggestion to fix issues (empty if pass)",
  "confidence": 0.0-1.0
}

Check for:
1. **Readability**: Are all labels/text clearly visible and not cut off?
2. **Overlap**: Are nodes/elements overlapping each other?
3. **Layout**: Is the layout logical (flow goes in intended direction)?
4. **Spacing**: Is there enough padding between elements?
5. **Completeness**: Are all expected nodes/connections present?
6. **Proportions**: Are elements reasonably sized (not too small, not too large)?

Pass if the diagram is clearly readable with no major issues.
Fail if labels are cut off, nodes overlap significantly, or layout is confusing.
"""

_VALIDATE_TEXT_SYSTEM = """\
You are a diagram quality reviewer. Examine the diagram definition source code and
assess whether it will produce a readable diagram.

Output ONLY valid JSON:
{
  "pass": true | false,
  "issues": ["list of specific issues found"],
  "suggestion": "brief actionable suggestion to fix issues (empty if pass)",
  "confidence": 0.0-1.0
}

Check for:
1. Are node labels concise enough to fit?
2. Is the direction/layout appropriate for the diagram type?
3. Are there too many nodes (>20 for sequence, >15 for flowchart)?
4. Are connections well-structured (no crossing lines, reasonable flow)?
"""


async def validate_diagram_render(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    renderer: str,
    definition: str,
    diagram_type: str,
    png_bytes: bytes | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Validate a rendered diagram using vision (if PNG available) or source code analysis.

    Returns dict with: pass (bool), issues (list), suggestion (str), confidence (float)
    Returns None if validation is unavailable (no vision model, no PNG).
    """
    if not cfg.enable_internal_llm or not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except Exception as exc:
        logger.warning("diagram_agent.langchain_unavailable: %s", exc)
        return None

    if png_bytes and len(png_bytes) > 0:
        # Vision-based validation
        import base64

        b64_image = base64.b64encode(png_bytes).decode("utf-8")
        messages = [
            SystemMessage(content=_VALIDATE_SYSTEM.strip()),
            HumanMessage(
                content=[
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64_image}",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Renderer: {renderer}\n"
                            f"Diagram type: {diagram_type}\n"
                            f"Validate this rendered diagram for readability."
                        ),
                    },
                ]
            ),
        ]
    else:
        # Text-based validation (no PNG available)
        messages = [
            SystemMessage(content=_VALIDATE_TEXT_SYSTEM.strip()),
            HumanMessage(
                content=(
                    f"Renderer: {renderer}\n"
                    f"Diagram type: {diagram_type}\n"
                    f"Definition:\n```\n{definition[:4000]}\n```\n"
                    f"Validate this diagram definition for readability."
                )
            ),
        ]

    started = time.perf_counter()
    llm_call_id = f"diagram_validate_{uuid4().hex[:16]}"
    try:
        async with httpx.AsyncClient(
            timeout=cfg.internal_llm_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as llm_http:
            llm_kwargs: dict[str, Any] = {
                "model": cfg.internal_llm_model,
                "api_key": cfg.internal_llm_api_key,
                "base_url": cfg.internal_llm_base_url,
                "http_async_client": llm_http,
            }
            llm = ChatOpenAI(**llm_kwargs)
            result = await llm.ainvoke(messages)
    except Exception as exc:
        logger.warning("diagram_agent.validation_llm_error: %s", exc)
        return None

    latency_ms = (time.perf_counter() - started) * 1000
    raw_content = (result.content or "").strip() if result else ""

    usage_meta = {}
    if result:
        usage_meta = serialize_usage_metadata(result)

    try:
        await post_usage_event(
            http_client=http_client,
            gateway_url=cfg.gateway_url,
            internal_token=cfg.gateway_internal_token,
            event=UsageEvent(
                llm_call_id=llm_call_id,
                llm_call_placed_at=time.time() - (latency_ms / 1000),
                agent_id="cosmic/diagram-agent:1.0.0",
                task_id=task_id or "",
                session_id=session_id or "",
                source=source or "",
                source_id=source_id or "",
                channel=channel or "",
                provider="openai_compatible",
                model=cfg.internal_llm_model,
                usage_kind="chat_completion",
                ok=True,
                latency_ms=latency_ms,
                usage=usage_meta,
            ),
        )
    except Exception:
        pass

    if not raw_content:
        return None

    # Parse JSON
    json_str = raw_content
    if "```" in json_str:
        parts = json_str.split("```")
        for part in parts:
            stripped = part.strip()
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                json_str = stripped
                break

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, dict) and "pass" in parsed:
            return parsed
    except json.JSONDecodeError:
        pass

    return None


async def regenerate_diagram_with_feedback(
    *,
    cfg: DiagramAgentConfig,
    http_client: httpx.AsyncClient,
    renderer: str,
    existing_definition: str,
    validation_issues: list[str],
    validation_suggestion: str,
    diagram_type: str = "",
    renderer_skill_context: str = "",
    task_id: str | None = None,
    session_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    channel: str | None = None,
) -> dict[str, Any] | None:
    """Regenerate a diagram definition based on validation feedback.

    Sends the existing definition + validation issues/suggestion to the LLM
    and asks it to produce a corrected version. Returns parsed JSON with
    {renderer, definition, reasoning}.
    """
    issues_text = (
        "\n".join(f"- {i}" for i in validation_issues)
        if validation_issues
        else "No specific issues listed."
    )
    context = (
        f"Renderer: {renderer}\n"
        f"Diagram type: {diagram_type}\n"
        f"Existing definition:\n```\n{existing_definition}\n```\n"
        f"Validation issues:\n{issues_text}\n"
        f"Suggestion: {validation_suggestion}\n"
        f"Regenerate a corrected definition that fixes these issues while keeping the same content."
    )
    if renderer_skill_context:
        context += f"\n---\n{renderer_skill_context}\n"

    return await invoke_diagram_internal_llm(
        cfg=cfg,
        http_client=http_client,
        system_content=_REGENERATE_SYSTEM,
        user_message=context,
        task_id=task_id,
        session_id=session_id,
        source=source,
        source_id=source_id,
        channel=channel,
    )
