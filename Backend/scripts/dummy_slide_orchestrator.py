"""
Local dummy orchestrator for the slide agent.

- Routes user text with Kimi (same Fireworks env as the slide agent) or simple rules.
- Sends signed TaskEnvelopes (sender cosmic/orchestrator:1.0.0) to SlideAgent.execute().
- LangGraph is ON by default. When the slide agent suspends for delegation, this script
  impersonates the real orchestrator: it fakes ``submit_reverse_task``, synthesizes a
  small ``reverse_result``, and immediately resumes with the same ``task_id`` — same
  back-and-forth pattern as production, without Redis or HTTP to a real orchestrator.
- Keeps a simple session log keyed by ``task_id`` (delegations, events, terminal status).

Environment (same as slide agent / test_fireworks_kimi):
  FIREWORKS_API_KEY or SLIDE_AGENT_MIMO_API_KEY
  FIREWORKS_KIMI_MODEL or SLIDE_AGENT_MIMO_MODEL (optional)
  AGENT_SECRET — task HMAC (default: dev-dummy-secret)

Run from repo root:
  python Backend/scripts/dummy_slide_orchestrator.py "3 slides on renewable energy"
  python Backend/scripts/dummy_slide_orchestrator.py --intent slide.edit --pptx C:\\deck.pptx --edit "Shorten slide 2"

Plain-English notes (see docstring history if you skipped the jargon):
- **Why we do not call agent.stop():** The slide agent class clears an internal HTTP
  client variable that the shared parent class expects during shutdown, so the stock
  ``stop()`` can error. We only close the client opened for slide work if it exists.
- **Standalone copy:** The same driver lives as ``run_local.py`` next to the slide
  agent in ``cosmic-slides`` (vendored ``shared/`` there). Use that folder for tests
  without this repo; use this script when working inside Cosmic-OS Backend.

Optional flags:
  --no-langgraph     Force the direct handlers (no suspend/resume loop).
  --max-rounds N     Cap delegate/resume iterations (default: 40).
  --dump-memory PATH Write task_id → event log JSON to a file.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_DIR.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from openai import OpenAI

from agents.slide_agent.agent import SlideAgent
from agents.slide_agent.config import SlideAgentConfig
from shared.agent_runtime import ORCHESTRATOR_AGENT_ID
from shared.contracts import (
    AgentResult,
    TaskEnvelope,
    TaskInProgress,
    generate_task_id,
    sign_task_envelope,
    utcnow,
)

logger = logging.getLogger(__name__)

# 1×1 PNG (grey) — placeholder for image.generate / diagram.create stubs
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

ROUTER_SYSTEM = """You are the COSMIC orchestrator routing layer for the slide agent only.
Given the user message (and optional context), reply with a single JSON object, no markdown:
{
  "intent": "slide.create" | "slide.edit" | "slide.recall_session",
  "input": { ... }
}

Rules:
- slide.create: input must include "description" (string). Optional: "template" (business-meeting, tech-trends, science-lesson, tech-infographics, blank), "data" (object).
- slide.edit: input must include "source_pptx_path" and "edit_request" (strings). If the user gives a path, use it; otherwise use CONTEXT_DEFAULT_PPTX if provided in the user message block.
- slide.recall_session: input must include "session_id" (string). Optional "limit" (integer). If missing session, use CONTEXT_SESSION_ID from the user message block.

Choose slide.edit when the user clearly wants to change an existing deck file.
Choose slide.recall_session only when they ask for history / previous decks in a session.
Otherwise slide.create.
"""


def _extract_json_object(text: str) -> dict:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def _rule_route(
    message: str,
    *,
    default_pptx: str | None,
    session_id: str,
) -> tuple[str, dict[str, object]]:
    lower = message.lower()
    if "recall" in lower or "history" in lower or "previous deck" in lower:
        return "slide.recall_session", {"session_id": session_id, "limit": 10}
    if default_pptx and (
        "edit" in lower
        or lower.endswith(".pptx")
        or "slide" in lower and "change" in lower
    ):
        return "slide.edit", {
            "source_pptx_path": default_pptx,
            "edit_request": message,
        }
    return "slide.create", {"description": message}


def _route_with_kimi(
    *,
    user_block: str,
    cfg: SlideAgentConfig,
) -> tuple[str, dict[str, object]]:
    if not cfg.mimo_api_key:
        raise RuntimeError(
            "No API key for routing: set FIREWORKS_API_KEY or SLIDE_AGENT_MIMO_API_KEY"
        )
    client = OpenAI(base_url=cfg.mimo_base_url, api_key=cfg.mimo_api_key)
    completion = client.chat.completions.create(
        model=cfg.mimo_model,
        messages=[
            {"role": "system", "content": ROUTER_SYSTEM},
            {"role": "user", "content": user_block},
        ],
        temperature=min(0.4, cfg.mimo_temperature),
        max_tokens=1024,
    )
    raw = (completion.choices[0].message.content or "").strip()
    data = _extract_json_object(raw)
    intent = str(data.get("intent") or "").strip()
    inp = data.get("input")
    if intent not in {"slide.create", "slide.edit", "slide.recall_session"}:
        raise ValueError(f"Router returned invalid intent: {intent!r}")
    if not isinstance(inp, dict):
        raise ValueError("Router must return input as a JSON object")
    return intent, inp


def _json_safe_for_signing(obj: Any) -> Any:
    """Resume input may embed image_bytes; HMAC canonical JSON cannot encode bytes."""
    if isinstance(obj, dict):
        return {str(k): _json_safe_for_signing(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe_for_signing(v) for v in obj]
    if isinstance(obj, tuple):
        return [_json_safe_for_signing(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    return obj


def _build_task(
    *,
    intent: str,
    input_obj: dict[str, object],
    session_id: str,
    agent_secret: str,
) -> TaskEnvelope:
    task_id = generate_task_id()
    idem = f"dummy_{uuid4().hex[:16]}"
    payload: dict[str, Any] = {
        "task_id": task_id,
        "task_list_id": "tasks:dummy-orchestrator",
        "session_id": session_id,
        "sender": ORCHESTRATOR_AGENT_ID,
        "recipient": "cosmic/slide-agent:1.0.0",
        "intent": intent,
        "input": input_obj,
        "idempotency_key": idem,
        "signature": "",
        "source": "user",
        "channel": "dummy-orchestrator",
    }
    sign_payload = {**payload, "input": _json_safe_for_signing(input_obj)}
    payload["signature"] = sign_task_envelope(sign_payload, agent_secret)
    return TaskEnvelope.model_validate(payload)


def _task_resign_input(
    task: TaskEnvelope,
    new_input: dict[str, Any],
    agent_secret: str,
) -> TaskEnvelope:
    payload = task.model_dump(mode="json")
    payload["input"] = new_input
    payload["signature"] = ""
    sign_payload = {**payload, "input": _json_safe_for_signing(new_input)}
    payload["signature"] = sign_task_envelope(sign_payload, agent_secret)
    return TaskEnvelope.model_validate(payload)


def _artifact_stub(
    *,
    path: Path,
    artifact_id: str,
    task_id: str,
    mime: str,
    created_by: str,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "artifact_id": artifact_id,
        "task_id": task_id,
        "mime": mime,
        "path": path.resolve().as_posix(),
        "sha256": "stub",
        "created_by_agent": created_by,
        "kind": "output",
        "audience": "deliverable",
    }


def _write_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_TINY_PNG)


def _synthesize_reverse_result(
    *,
    target_intent: str,
    target_input: dict[str, Any],
    input_artifacts: list[dict[str, Any]],
    task_id: str,
    rev_id: str,
    cfg: SlideAgentConfig,
) -> dict[str, Any]:
    """Build a minimal reverse_result dict the slide LangGraph resume path accepts."""
    ti = (target_intent or "").strip().lower()
    root = (cfg.artifacts_root / "dummy_orchestrator_stub" / task_id / rev_id).resolve()
    root.mkdir(parents=True, exist_ok=True)

    if ti in {"image.generate", "diagram.create"}:
        png_path = root / "stub.png"
        _write_png(png_path)
        return {
            "status": "completed",
            "output": {},
            "artifacts": [
                _artifact_stub(
                    path=png_path,
                    artifact_id=f"art_stub_{rev_id}",
                    task_id=task_id,
                    mime="image/png",
                    created_by=cfg.image_agent_id
                    if ti == "image.generate"
                    else cfg.diagram_agent_id,
                )
            ],
        }

    if ti == "docs.parse_bundle":
        bundle_dir = root / "docs_parser" / "bundle_0"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        manifest = bundle_dir / "manifest.json"
        chunk_index = bundle_dir / "chunk_index.json"
        manifest.write_text(
            json.dumps(
                {
                    "bundle_id": "dummy-bundle",
                    "doc_id": "dummy-doc",
                    "title": "Dummy parse (orchestrator stub)",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        chunk_index.write_text(
            json.dumps(
                {
                    "pages": [],
                    "figures": [],
                    "slides": [],
                    "assets": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        docs_out: list[dict[str, Any]] = []
        for art in input_artifacts:
            aid = str(art.get("artifact_id") or "").strip()
            if not aid:
                continue
            fn = str(art.get("filename") or Path(str(art.get("path") or "")).name)
            docs_out.append(
                {
                    "artifact_id": aid,
                    "title": fn or "Uploaded document",
                    "summary": (
                        "Stub: dummy orchestrator did not parse file contents. "
                        "Replace this stub with a real docs.parse_bundle result for fidelity."
                    ),
                }
            )
        if not docs_out:
            docs_out.append(
                {
                    "artifact_id": "stub-doc",
                    "title": "Stub document",
                    "summary": "No input artifacts; dummy docs.parse_bundle stub.",
                }
            )
        return {
            "status": "completed",
            "output": {"documents": docs_out},
            "artifacts": [
                _artifact_stub(
                    path=manifest,
                    artifact_id=f"art_manifest_{rev_id}",
                    task_id=task_id,
                    mime="application/json",
                    created_by=cfg.docs_parser_agent_id,
                ),
                _artifact_stub(
                    path=chunk_index,
                    artifact_id=f"art_chunk_{rev_id}",
                    task_id=task_id,
                    mime="application/json",
                    created_by=cfg.docs_parser_agent_id,
                ),
            ],
        }

    if ti == "docs.search_bundle":
        return {
            "status": "completed",
            "output": {
                "bundle_id": "dummy-bundle",
                "doc_id": None,
                "query": target_input.get("query"),
                "search_kind": "semantic",
                "matches": [],
            },
        }

    if ti == "docs.read_bundle":
        return {
            "status": "completed",
            "output": {
                "bundle_id": target_input.get("bundle_id") or "dummy-bundle",
                "doc_id": target_input.get("doc_id"),
                "title": "Stub excerpt",
                "mode": "section",
                "content": (
                    "Stub text from dummy orchestrator (docs.read_bundle). "
                    "No real bundle content was retrieved."
                ),
                "citations": [],
            },
        }

    if ti == "docs.fetch_asset":
        png_path = root / "fetch_stub.png"
        _write_png(png_path)
        return {
            "status": "completed",
            "output": {
                "bundle_id": target_input.get("bundle_id"),
                "doc_id": target_input.get("doc_id"),
                "asset_id": target_input.get("asset_id"),
                "content": "Stub asset fetch.",
                "asset": {"kind": "figure_image"},
                "analysis": {"summary": "Stub analysis."},
            },
            "artifacts": [
                _artifact_stub(
                    path=png_path,
                    artifact_id=f"art_fetch_{rev_id}",
                    task_id=task_id,
                    mime="image/png",
                    created_by=cfg.docs_parser_agent_id,
                )
            ],
        }

    if ti in {"docs.reinspect_asset", "docs.reinspect"}:
        return {
            "status": "completed",
            "output": {
                "bundle_id": target_input.get("bundle_id"),
                "doc_id": target_input.get("doc_id"),
                "asset_id": target_input.get("asset_id"),
                "question": target_input.get("question"),
                "analysis": {
                    "summary": "Stub reinspection (dummy orchestrator).",
                    "visible_text": [],
                },
            },
        }

    return {
        "status": "failed",
        "error": {
            "code": "DUMMY_ORCHESTRATOR_NO_STUB",
            "message": (
                f"No stub for delegated intent {target_intent!r}. "
                "Extend _synthesize_reverse_result in dummy_slide_orchestrator.py."
            ),
        },
    }


def _print_result(result: AgentResult | TaskInProgress) -> None:
    print(json.dumps(result.model_dump(mode="json"), indent=2))


def _append_memory(
    memory: dict[str, list[dict[str, Any]]],
    task_id: str,
    event: dict[str, Any],
) -> None:
    memory.setdefault(task_id, []).append(event)


async def _run_with_delegate_loop(
    *,
    agent: SlideAgent,
    task: TaskEnvelope,
    agent_secret: str,
    session_memory: dict[str, list[dict[str, Any]]],
    max_rounds: int,
) -> AgentResult | TaskInProgress:
    """Execute task; on TaskInProgress, inject synthetic reverse_result and resume."""
    original_input = dict(task.input)
    pending: dict[str, Any] | None = None
    rounds = 0

    async def patched_submit_reverse_task(
        self: SlideAgent,
        *,
        current_task: TaskEnvelope,
        intent: str,
        input_payload: dict[str, Any] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
        priority: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        nonlocal pending
        del priority, idempotency_key
        if intent != "orchestrator.delegate":
            raise RuntimeError(
                f"dummy orchestrator only stubs orchestrator.delegate; got {intent!r}"
            )
        inner = dict(input_payload or {})
        target_intent = str(inner.get("target_intent") or "").strip()
        resume_state = dict(inner.get("resume_payload") or {})
        target_agent_id = str(inner.get("target_agent_id") or "").strip()
        rev_id = f"rev_{uuid4().hex[:12]}"
        arts = [a for a in (input_artifacts or []) if isinstance(a, dict)]
        reverse_result = _synthesize_reverse_result(
            target_intent=target_intent,
            target_input=dict(inner.get("target_input") or {}),
            input_artifacts=arts,
            task_id=current_task.task_id,
            rev_id=rev_id,
            cfg=self._cfg,
        )
        pending = {
            "resume_state": resume_state,
            "reverse_result": reverse_result,
            "reverse_task": {
                "reverse_task_id": rev_id,
                "target_intent": target_intent,
                "target_agent_id": target_agent_id,
            },
        }
        _append_memory(
            session_memory,
            current_task.task_id,
            {
                "kind": "delegation_stub",
                "target_intent": target_intent,
                "reverse_task_id": rev_id,
                "reverse_status": reverse_result.get("status"),
            },
        )
        logger.info(
            "Stubbed delegate: task=%s target=%s rev=%s",
            current_task.task_id,
            target_intent,
            rev_id,
        )
        return {"ok": True, "reverse_task_id": rev_id, "status": "registered"}

    async def patched_emit_event(
        self: SlideAgent,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> str:
        _append_memory(
            session_memory,
            task_id,
            {"kind": "emit", "event_type": event_type, "payload": payload},
        )
        return f"evt_stub_{uuid4().hex[:8]}"

    agent.submit_reverse_task = types.MethodType(  # type: ignore[method-assign]
        patched_submit_reverse_task,
        agent,
    )
    agent.emit_event = types.MethodType(patched_emit_event, agent)  # type: ignore[method-assign]

    current = task
    while rounds < max_rounds:
        logger.info(
            "execute round=%s task_id=%s intent=%s",
            rounds,
            current.task_id,
            current.intent,
        )
        result = await agent.execute(current)
        if isinstance(result, AgentResult):
            _append_memory(
                session_memory,
                current.task_id,
                {
                    "kind": "terminal",
                    "status": result.status,
                    "error_code": result.error.code if result.error else None,
                },
            )
            return result
        if not isinstance(result, TaskInProgress):
            return result
        if not pending:
            logger.error(
                "TaskInProgress but no pending delegation captured for %s",
                current.task_id,
            )
            _append_memory(
                session_memory,
                current.task_id,
                {"kind": "error", "message": "suspended_without_pending"},
            )
            return result
        resume_block = {
            "resume_state": pending["resume_state"],
            "reverse_result": pending["reverse_result"],
            "reverse_task": pending["reverse_task"],
        }
        pending = None
        merged_input = {k: v for k, v in original_input.items() if k != "_resume"}
        merged_input["_resume"] = resume_block
        current = _task_resign_input(current, merged_input, agent_secret)
        _append_memory(
            session_memory,
            current.task_id,
            {"kind": "resume", "round": rounds + 1},
        )
        rounds += 1

    logger.error("Exceeded --max-rounds=%s", max_rounds)
    _append_memory(
        session_memory,
        current.task_id,
        {"kind": "error", "message": "max_rounds_exceeded"},
    )
    return TaskInProgress(
        task_id=current.task_id,
        idempotency_key=current.idempotency_key,
        executing_since=utcnow(),
        check_after_sec=10,
    )


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(description="Dummy orchestrator → slide agent")
    parser.add_argument(
        "message",
        nargs="*",
        help="User message (natural language). Ignored if --intent is used with full args.",
    )
    parser.add_argument(
        "--intent",
        choices=("slide.create", "slide.edit", "slide.recall_session"),
        help="Skip Kimi routing and call this intent directly.",
    )
    parser.add_argument("--description", help="slide.create: deck description")
    parser.add_argument("--template", help="slide.create: template id")
    parser.add_argument("--pptx", help="slide.edit: path to source PPTX")
    parser.add_argument("--edit", help="slide.edit: edit instructions")
    parser.add_argument(
        "--session-id",
        default="dummy-orchestrator",
        help="TaskEnvelope.session_id and default for recall",
    )
    parser.add_argument(
        "--recall-session-id",
        help="slide.recall_session: session to query (default: --session-id)",
    )
    parser.add_argument(
        "--no-kimi-route",
        action="store_true",
        help="Use simple keyword routing instead of Kimi (no extra LLM call).",
    )
    parser.add_argument(
        "--no-langgraph",
        action="store_true",
        help="Disable LangGraph (direct handlers only, no delegate loop).",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=40,
        help="Max delegate/resume iterations when LangGraph is on (default: 40).",
    )
    parser.add_argument(
        "--dump-memory",
        type=Path,
        help="Write session memory JSON (task_id -> event list) to this path.",
    )
    args = parser.parse_args()

    cfg = SlideAgentConfig.from_env()
    if args.no_langgraph:
        cfg = replace(cfg, slide_use_langgraph=False)

    agent_secret = os.getenv("AGENT_SECRET", "dev-dummy-secret").strip()

    message = " ".join(args.message).strip()
    intent: str
    input_obj: dict[str, object]

    if args.intent:
        intent = args.intent
        if intent == "slide.create":
            desc = args.description or message
            if not desc:
                parser.error("slide.create needs --description and/or a message")
            input_obj = {"description": desc}
            if args.template:
                input_obj["template"] = args.template
        elif intent == "slide.edit":
            pptx = args.pptx or ""
            edit = args.edit or message
            if not pptx or not edit:
                parser.error("slide.edit needs --pptx and --edit (and/or message as edit text)")
            input_obj = {"source_pptx_path": pptx, "edit_request": edit}
        else:
            sid = args.recall_session_id or args.session_id
            input_obj = {"session_id": sid, "limit": 10}
    else:
        if not message:
            parser.error("Provide a user message or use --intent with explicit fields")
        ctx_lines = [
            f'CONTEXT_SESSION_ID: "{args.session_id}"',
        ]
        if args.pptx:
            ctx_lines.append(f'CONTEXT_DEFAULT_PPTX: "{args.pptx}"')
        user_block = "\n".join(ctx_lines) + "\n\nUSER_MESSAGE:\n" + message

        if args.no_kimi_route:
            intent, input_obj = _rule_route(
                message,
                default_pptx=args.pptx,
                session_id=args.session_id,
            )
        else:
            try:
                intent, input_obj = _route_with_kimi(user_block=user_block, cfg=cfg)
            except Exception as exc:
                logger.warning("Kimi routing failed (%s); falling back to rules", exc)
                intent, input_obj = _rule_route(
                    message,
                    default_pptx=args.pptx,
                    session_id=args.session_id,
                )

    # LangGraph workflow only models slide.create / slide.edit; recall must use
    # the direct handler or analyze_request mis-routes into plan_deck.
    if intent == "slide.recall_session":
        cfg = replace(cfg, slide_use_langgraph=False)

    task = _build_task(
        intent=intent,
        input_obj=input_obj,
        session_id=args.session_id,
        agent_secret=agent_secret,
    )

    session_memory: dict[str, list[dict[str, Any]]] = {}

    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.xadd = AsyncMock(return_value="0-0")
    redis.rpush = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)

    agent = SlideAgent(redis_client=redis, config=cfg)
    await agent.on_startup()

    if cfg.slide_use_langgraph and intent in {"slide.create", "slide.edit"}:
        result = await _run_with_delegate_loop(
            agent=agent,
            task=task,
            agent_secret=agent_secret,
            session_memory=session_memory,
            max_rounds=max(1, args.max_rounds),
        )
    else:
        logger.info("Dispatching intent=%s task_id=%s", task.intent, task.task_id)
        result = await agent.execute(task)
        if isinstance(result, AgentResult):
            _append_memory(
                session_memory,
                task.task_id,
                {"kind": "terminal", "status": result.status},
            )

    _print_result(result)

    if args.dump_memory:
        args.dump_memory.write_text(
            json.dumps(session_memory, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Wrote session memory to %s", args.dump_memory)

    logger.info(
        "Session memory (task %s): %s events",
        task.task_id,
        len(session_memory.get(task.task_id, [])),
    )
    for ev in session_memory.get(task.task_id, []):
        logger.debug("memory: %s", ev)

    hc = getattr(agent, "_http_client", None)
    if hc is not None:
        try:
            await hc.aclose()
        except Exception:
            pass

    if isinstance(result, AgentResult):
        return 0 if result.status == "completed" else 1
    return 1


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
