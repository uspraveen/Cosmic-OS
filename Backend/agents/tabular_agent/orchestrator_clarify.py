"""
Mid-task clarification via the **orchestrator** task-input relay (COSMIC architecture §3.12 / §13.1).

Publishes to ``user_input:requests`` (through ``POST /internal/tasks/{task_id}/request-input``) for the
**parent orchestrator task** (``TaskEnvelope.parent_task_id``). The Gateway surfaces the request; the user
replies through the normal ``user_input:replies`` path; the orchestrator resolves the wait.

This is **not** conversational ``<awaiting_reply/>`` routing — it uses the task input request/reply streams.

.. note::
   The shared COSMIC runtime now supports a true second-invocation resume path for suspended specialist
   work. This helper only publishes the input request through the orchestrator; the resumed child task is
   dispatched later by the orchestrator after the user reply arrives.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import TabularAgentConfig

logger = logging.getLogger(__name__)


async def request_orchestrator_task_input(
    *,
    cfg: TabularAgentConfig,
    http_client: httpx.AsyncClient,
    parent_task_id: str,
    question: str,
    options: list[str],
    channel: str | None,
    wait_timeout_sec: float,
    specialist_agent_id: str = "cosmic/tabular-agent:1.0.0",
) -> dict[str, Any]:
    """
    Call orchestrator ``POST /internal/tasks/{parent_task_id}/request-input``.

    Requires ``cfg.orchestrator_url`` and ``cfg.orchestrator_internal_token`` (or env fallbacks).
    """
    base = str(cfg.orchestrator_url or "").strip().rstrip("/")
    token = str(cfg.orchestrator_internal_token or "").strip()
    if not base:
        raise RuntimeError("orchestrator_url is not configured (TABULAR_AGENT_ORCHESTRATOR_URL).")
    if not token:
        raise RuntimeError("orchestrator_internal_token is not configured (TABULAR_AGENT_ORCHESTRATOR_INTERNAL_TOKEN).")

    url = f"{base}/internal/tasks/{parent_task_id}/request-input"
    body: dict[str, Any] = {
        "question": question.strip(),
        "options": [str(o).strip() for o in options if str(o).strip()][:12],
        "agent": specialist_agent_id,
        "wait_timeout_sec": float(wait_timeout_sec),
    }
    if channel:
        body["channel"] = channel

    headers = {"X-Internal-Token": token, "Content-Type": "application/json"}
    timeout = httpx.Timeout(max(60.0, wait_timeout_sec + 30.0), connect=15.0)
    resp = await http_client.post(url, json=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("orchestrator request-input returned non-object JSON.")
    if data.get("ok") is False:
        raise RuntimeError(str(data.get("error") or "orchestrator request-input failed"))
    return data
