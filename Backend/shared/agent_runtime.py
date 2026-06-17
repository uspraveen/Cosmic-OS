from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import redis.asyncio as redis
import yaml

from registry.live_state import register_intent_index, write_heartbeat
from registry.store import RegistryStore

from .contracts import (
    SOURCE_PRIORITY_MAP,
    AgentError,
    AgentResult,
    EventEnvelope,
    Heartbeat,
    TaskEnvelope,
    TaskInProgress,
    generate_task_id,
    sign_task_envelope,
    verify_task_envelope,
)
from .idempotency import RESULT_TTL_SEC, execute_with_idempotency
from .memory_tools import MemoryRead, MemoryWrite
from .redis_bus import EVENTS_STREAM_MAXLEN, parse_task_envelope, task_stream_name
from .redis_client import ensure_stream_group
from .step_plan import StepPlan

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
WORKER_GROUP = "workers"
STREAM_PRIORITIES = ("high", "normal", "low")
ORCHESTRATOR_AGENT_ID = "cosmic/orchestrator:1.0.0"


class AgentRuntime:
    """Shared runtime backbone for future specialist agents."""

    def __init__(
        self,
        *,
        agent_card_path: str | Path,
        redis_client: redis.Redis,
        instance_id: str | None = None,
        agent_secret: str | None = None,
        registry_db_path: str | Path | None = None,
        gateway_url: str | None = None,
        gateway_internal_token: str | None = None,
        orchestrator_url: str | None = None,
        orchestrator_internal_token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.agent_card_path = Path(agent_card_path)
        self.redis = redis_client
        self.agent_secret = (agent_secret or os.getenv("AGENT_SECRET", "")).strip()
        self.gateway_url = (gateway_url or os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")).strip()
        self.gateway_internal_token = (gateway_internal_token or os.getenv("GATEWAY_INTERNAL_TOKEN", "")).strip()
        self.orchestrator_url = (
            orchestrator_url or os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8743")
        ).strip()
        self.orchestrator_internal_token = (
            orchestrator_internal_token
            or os.getenv("ORCHESTRATOR_INTERNAL_TOKEN")
            or self.gateway_internal_token
        ).strip()
        self.instance_id = (instance_id or os.getenv("INSTANCE_ID", "")).strip() or f"inst-{id(self)}"
        timeout = httpx.Timeout(30.0, connect=10.0)
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout, http2=True)
        self._owns_http_client = http_client is None

        self.agent_card = self._load_agent_card(self.agent_card_path)
        self.agent_id = str(self.agent_card["agent_id"]).strip()
        self.display_name = str(self.agent_card.get("display_name") or self.agent_id).strip() or self.agent_id
        self.stream_key = str(self.agent_card.get("stream_key") or f"streams:{self.agent_id}").strip()

        sla = self.agent_card.get("sla") if isinstance(self.agent_card.get("sla"), dict) else {}
        self.max_concurrency = max(1, int(sla.get("max_concurrency") or 1))
        self.heartbeat_interval_sec = max(1, int(sla.get("heartbeat_interval_sec") or 10))
        self.heartbeat_ttl_sec = max(1, int(sla.get("heartbeat_ttl_sec") or 30))
        self.max_task_duration_sec = max(1, int(sla.get("max_task_duration_sec") or 300))
        self.claim_min_idle_ms = self.max_task_duration_sec * 2 * 1000
        self.provider_health_probe_interval_sec = self._safe_positive_int(
            sla.get("provider_health_probe_interval_sec")
            or os.getenv("AGENT_PROVIDER_HEALTH_PROBE_INTERVAL_SEC", "300"),
            fallback=300,
            minimum=30,
        )

        policies = self.agent_card.get("policies") if isinstance(self.agent_card.get("policies"), dict) else {}
        raw_allowed_senders = policies.get("allowed_senders") if isinstance(policies.get("allowed_senders"), list) else []
        self.allowed_senders = {str(item).strip() for item in raw_allowed_senders if str(item).strip()}
        raw_authorization = policies.get("intent_authorization") if isinstance(policies.get("intent_authorization"), dict) else {}
        self.intent_authorization = {
            str(intent).strip(): {
                str(item).strip()
                for item in values
                if str(item).strip()
            }
            for intent, values in raw_authorization.items()
            if isinstance(values, list) and str(intent).strip()
        }

        registry_path = Path(registry_db_path).expanduser() if registry_db_path else BACKEND_ROOT / "registry" / "registry.db"
        self.registry_store = RegistryStore(registry_path)

        self.started = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._active_task_count = 0

        self.auth: dict[str, Any] | None = None
        self.step_plan: StepPlan | None = None
        self.memory_read: MemoryRead | None = None
        self.memory_write: MemoryWrite | None = None
        self._provider_health_probe_cache: dict[str, Any] | None = None
        self._provider_health_probe_last_at = 0.0

    async def on_startup(self) -> None:
        return None

    async def execute(self, task: TaskEnvelope) -> AgentResult | TaskInProgress:
        raise NotImplementedError

    async def register(self) -> None:
        self.registry_store.initialize()
        self.registry_store.upsert_agent_card(self.agent_card)
        await register_intent_index(self.agent_id, self.agent_card, self.redis)

        for priority in STREAM_PRIORITIES:
            await ensure_stream_group(
                self.redis,
                stream=task_stream_name(self.agent_id, priority),
                group=WORKER_GROUP,
            )

        await self._publish_heartbeat(healthy=False, status="starting")
        await self.on_startup()
        await self._publish_heartbeat(healthy=True, status="healthy")
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"{self.agent_id}-heartbeat",
        )
        self.started = True

    async def run(self) -> None:
        while True:
            handled = await self.poll_once()
            if not handled:
                await asyncio.sleep(0.05)

    async def poll_once(self) -> bool:
        for priority in STREAM_PRIORITIES:
            stream = task_stream_name(self.agent_id, priority)
            claimed = await self._claim_stale_messages(stream)
            if claimed:
                return True

        for priority in STREAM_PRIORITIES:
            stream = task_stream_name(self.agent_id, priority)
            messages = await self.redis.xreadgroup(
                groupname=WORKER_GROUP,
                consumername=self.instance_id,
                streams={stream: ">"},
                count=1,
                block=10,
            )
            for stream_name, items in messages:
                for message_id, fields in items:
                    await self._process_message(message_id, fields, stream_name)
                    return True
        return False

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self.memory_read is not None:
            await self.memory_read.close()
            self.memory_read = None
        if self.memory_write is not None:
            await self.memory_write.close()
            self.memory_write = None
        if self._owns_http_client:
            await self._http_client.aclose()
        self.started = False

    async def emit_event(self, task_id: str, event_type: str, payload: dict[str, Any]) -> str:
        seq = int(await self.redis.incr(f"event_seq:{task_id}"))
        event = EventEnvelope(
            task_id=task_id,
            agent_id=self.agent_id,
            event_type=event_type,
            seq=seq,
            payload=payload,
        )
        message_id = await self.redis.xadd(
            "streams:events",
            {"event": event.model_dump_json()},
            maxlen=EVENTS_STREAM_MAXLEN,
            approximate=True,
        )
        await self.redis.rpush(f"task_events:{task_id}", message_id)
        if event_type in {"task.completed", "task.failed", "task.cancelled", "task.dlq"}:
            await self.redis.expire(f"event_seq:{task_id}", RESULT_TTL_SEC)
            await self.redis.expire(f"task_events:{task_id}", RESULT_TTL_SEC)
        return message_id

    async def emit_terminal_event(self, task_id: str, result: AgentResult) -> str:
        payload: dict[str, Any] = {
            "status": result.status,
            "output": result.output,
            "artifacts": [item.model_dump(mode="json") for item in result.artifacts],
        }
        if result.error is not None:
            payload["error"] = result.error.model_dump(mode="json")
        if result.status == "completed":
            event_type = "task.completed"
        elif result.error is not None and result.error.code == "CANCELLED":
            event_type = "task.cancelled"
        else:
            event_type = "task.failed"
        return await self.emit_event(task_id, event_type, payload)

    async def submit_reverse_task(
        self,
        *,
        current_task: TaskEnvelope,
        intent: str,
        input_payload: dict[str, Any] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
        priority: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        normalized_intent = str(intent or "").strip()
        if not normalized_intent:
            raise RuntimeError("intent is required for reverse tasks.")
        if not self.orchestrator_url:
            raise RuntimeError("orchestrator_url is not configured.")
        if not self.orchestrator_internal_token:
            raise RuntimeError("orchestrator_internal_token is not configured.")
        if not self.agent_secret:
            raise RuntimeError("AGENT_SECRET is required for reverse tasks.")

        reverse_input = dict(input_payload or {})
        if "request_id" not in reverse_input:
            inherited_request_id = str(current_task.input.get("request_id") or "").strip()
            if inherited_request_id:
                reverse_input["request_id"] = inherited_request_id

        normalized_priority = str(priority or current_task.priority or SOURCE_PRIORITY_MAP["agent"]).strip()
        if normalized_priority not in {"high", "normal", "low"}:
            normalized_priority = SOURCE_PRIORITY_MAP["agent"]

        reverse_task = TaskEnvelope(
            task_id=generate_task_id(),
            task_list_id=current_task.task_list_id,
            parent_task_id=current_task.task_id,
            session_id=current_task.session_id,
            sender=self.agent_id,
            recipient=ORCHESTRATOR_AGENT_ID,
            intent=normalized_intent,
            input=reverse_input,
            input_artifacts=[item for item in (input_artifacts or []) if isinstance(item, dict)],
            idempotency_key=str(idempotency_key or "").strip()
            or self._build_reverse_idempotency_key(current_task.idempotency_key, normalized_intent, reverse_input),
            deadline_ts=current_task.deadline_ts,
            priority=normalized_priority,
            leader_epoch=None,
            signature="",
            source="agent",
            source_id=self.agent_id,
            channel=current_task.channel,
        )
        signature = sign_task_envelope(reverse_task, self.agent_secret)
        signed_task = reverse_task.model_copy(update={"signature": signature})
        url = f"{self.orchestrator_url.rstrip('/')}/internal/reverse-tasks"
        response = await self._http_client.post(
            url,
            json=signed_task.model_dump(mode="json"),
            headers={
                "X-Internal-Token": self.orchestrator_internal_token,
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("orchestrator reverse-task response must be a JSON object.")
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error") or "reverse-task submission failed"))
        return payload

    async def request_orchestrator_delegate(
        self,
        *,
        current_task: TaskEnvelope,
        target_intent: str,
        target_input: dict[str, Any] | None = None,
        target_agent_id: str | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
        resume_payload: dict[str, Any] | None = None,
        resume_intent: str = "agent.resume",
        reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_intent": str(target_intent or "").strip(),
            "target_input": dict(target_input or {}),
            "resume_payload": dict(resume_payload or {}),
            "resume_intent": str(resume_intent or "agent.resume").strip() or "agent.resume",
        }
        normalized_agent_id = str(target_agent_id or "").strip()
        if normalized_agent_id:
            payload["target_agent_id"] = normalized_agent_id
        normalized_reason = str(reason or "").strip()
        if normalized_reason:
            payload["reason"] = normalized_reason
        return await self.submit_reverse_task(
            current_task=current_task,
            intent="orchestrator.delegate",
            input_payload=payload,
            input_artifacts=input_artifacts,
        )

    async def _heartbeat_loop(self) -> None:
        while True:
            await self._publish_heartbeat(healthy=True, status="healthy")
            await asyncio.sleep(self.heartbeat_interval_sec)

    async def _claim_stale_messages(self, stream: str) -> bool:
        if not hasattr(self.redis, "xautoclaim"):
            return False
        claimed = await self.redis.xautoclaim(
            stream,
            WORKER_GROUP,
            self.instance_id,
            min_idle_time=self.claim_min_idle_ms,
            start_id="0-0",
            count=1,
        )
        messages = self._extract_claimed_messages(claimed)
        for message_id, fields in messages:
            await self._process_message(message_id, fields, stream)
            return True
        return False

    async def _process_message(self, message_id: str, fields: dict[str, Any], stream: str) -> None:
        try:
            task = parse_task_envelope(fields)
        except Exception as exc:
            logger.warning("agent.invalid_stream_entry stream=%s message_id=%s error=%s", stream, message_id, exc)
            await self.redis.xack(stream, WORKER_GROUP, message_id)
            return
        await self._handle_task(task, message_id, stream)

    async def _handle_task(self, task: TaskEnvelope, message_id: str, stream: str) -> None:
        if task.recipient != self.agent_id:
            await self.redis.xack(stream, WORKER_GROUP, message_id)
            return

        if not self.agent_secret or not verify_task_envelope(task, self.agent_secret):
            await self.emit_event(
                task.task_id,
                "task.rejected",
                {"reason": "invalid_signature", "sender": task.sender, "intent": task.intent},
            )
            await self.redis.xack(stream, WORKER_GROUP, message_id)
            return

        task_input = dict(task.input)
        self.auth = task_input.pop("auth", None) if isinstance(task_input.get("auth"), dict) else None
        provisional_task = task.model_copy(update={"input": task_input})
        try:
            working_task = self._inflate_resume_task(provisional_task)
        except Exception as exc:
            await self.emit_event(
                task.task_id,
                "task.rejected",
                {"reason": "invalid_resume_payload", "sender": task.sender, "intent": task.intent, "error": str(exc)[:400]},
            )
            await self.redis.xack(stream, WORKER_GROUP, message_id)
            self.auth = None
            return

        if not self._sender_allowed(working_task):
            await self.emit_event(
                task.task_id,
                "task.rejected",
                {"reason": "unauthorized_sender", "sender": task.sender, "intent": working_task.intent},
            )
            await self.redis.xack(stream, WORKER_GROUP, message_id)
            self.auth = None
            return

        await self.emit_event(
            task.task_id,
            "task.accepted",
            {"sender": task.sender, "intent": working_task.intent, "stream": stream},
        )
        self.step_plan = StepPlan(task_id=task.task_id, emit_event_fn=self.emit_event)
        self.memory_read = MemoryRead(
            gateway_url=self.gateway_url,
            service_token=self.gateway_internal_token,
            agent_id=self.agent_id,
            client=self._http_client,
        )
        self.memory_write = MemoryWrite(
            gateway_url=self.gateway_url,
            service_token=self.gateway_internal_token,
            agent_id=self.agent_id,
            client=self._http_client,
        )

        self._active_task_count += 1
        await self._publish_heartbeat(healthy=True, status="healthy")
        try:
            result = await execute_with_idempotency(
                working_task,
                self.execute,
                self.redis,
                agent_max_duration_sec=self.max_task_duration_sec,
            )
            if isinstance(result, AgentResult):
                if result.status == "completed" and self.step_plan.has_pending_steps():
                    result = AgentResult(
                        status="failed",
                        output={},
                        artifacts=[],
                        error=AgentError(
                            code="PLAN_INCOMPLETE",
                            retryable=False,
                            message="Agent returned completed but StepPlan has pending steps.",
                            next_action="escalate",
                        ),
                    )
                await self.emit_terminal_event(task.task_id, result)
                await self.redis.xack(stream, WORKER_GROUP, message_id)
                return

            await self.emit_event(task.task_id, "task.deferred", result.model_dump(mode="json"))
            await self.redis.xack(stream, WORKER_GROUP, message_id)
        except Exception as exc:
            logger.exception("agent.task_failed agent_id=%s task_id=%s", self.agent_id, task.task_id)
            await self.emit_terminal_event(
                task.task_id,
                AgentResult(
                    status="failed",
                    output={},
                    artifacts=[],
                    error=AgentError(
                        code="INTERNAL_ERROR",
                        retryable=False,
                        message=str(exc).strip()[:500] or "Agent execution failed.",
                        next_action="escalate",
                    ),
                ),
            )
            await self.redis.xack(stream, WORKER_GROUP, message_id)
        finally:
            self._active_task_count = max(0, self._active_task_count - 1)
            await self._publish_heartbeat(healthy=True, status="healthy")
            self.auth = None
            self.step_plan = None
            self.memory_read = None
            self.memory_write = None

    def _inflate_resume_task(self, task: TaskEnvelope) -> TaskEnvelope:
        if task.intent != "agent.resume":
            return task

        resume_intent = str(task.input.get("resume_intent") or "").strip()
        resume_input = task.input.get("resume_input")
        resume_reply = task.input.get("reply") if isinstance(task.input.get("reply"), dict) else {}
        resume_state = task.input.get("resume_state") if isinstance(task.input.get("resume_state"), dict) else {}
        resume_of_task_id = str(task.input.get("resume_of_task_id") or "").strip() or None
        input_request_id = str(task.input.get("input_request_id") or "").strip() or None

        if not resume_intent:
            raise ValueError("agent.resume requires resume_intent")
        if not isinstance(resume_input, dict):
            raise ValueError("agent.resume requires resume_input object")

        merged_input = dict(resume_input)
        extra_resume_fields = {
            key: value
            for key, value in task.input.items()
            if key
            not in {
                "resume_of_task_id",
                "resume_intent",
                "resume_input",
                "reply",
                "resume_state",
                "input_request_id",
            }
        }
        merged_input["_resume"] = {
            "resume_of_task_id": resume_of_task_id,
            "input_request_id": input_request_id,
            "reply": dict(resume_reply),
            "resume_state": dict(resume_state),
            **extra_resume_fields,
        }
        return task.model_copy(update={"intent": resume_intent, "input": merged_input})

    async def _publish_heartbeat(self, *, healthy: bool, status: str | None = None) -> None:
        heartbeat_healthy = healthy
        heartbeat_status = status
        health_details: dict[str, Any] | None = None
        if healthy:
            health_details = await self.provider_health_probe()
            if health_details:
                heartbeat_status = str(health_details.get("status") or heartbeat_status or "healthy")
                heartbeat_healthy = bool(health_details.get("available", health_details.get("healthy", True)))
        await write_heartbeat(
            self._heartbeat(healthy=heartbeat_healthy),
            self.redis,
            status=heartbeat_status,
            details=health_details,
        )

    async def provider_health_probe(self) -> dict[str, Any] | None:
        """Return provider/auth health for heartbeat state.

        Specialist processes do not own Google refresh tokens; Gateway does.
        For Google-backed agents, this asks Gateway to verify account auth and
        run a tiny scoped provider call. Results are cached so 10s heartbeats
        do not hammer Google APIs.
        """
        probe_config = self._google_provider_health_probe_config()
        if probe_config is None:
            return None
        now = asyncio.get_running_loop().time()
        if (
            self._provider_health_probe_cache is not None
            and now - self._provider_health_probe_last_at < self.provider_health_probe_interval_sec
        ):
            return dict(self._provider_health_probe_cache)
        probe = await self._run_google_provider_health_probe(probe_config)
        self._provider_health_probe_cache = dict(probe)
        self._provider_health_probe_last_at = now
        return probe

    async def _run_google_provider_health_probe(self, probe_config: dict[str, Any]) -> dict[str, Any]:
        if not self.gateway_url or not self.gateway_internal_token:
            return {
                "status": "degraded",
                "healthy": False,
                "available": True,
                "provider": "google",
                "tool": probe_config.get("tool"),
                "reason": "gateway_credentials_probe_unconfigured",
            }
        url = f"{self.gateway_url.rstrip('/')}/internal/credentials/google/auth-health"
        try:
            response = await self._http_client.post(
                url,
                json={
                    "agent_id": self.agent_id,
                    "tool": probe_config["tool"],
                    "required_scopes": probe_config["required_scopes"],
                },
                headers={
                    "X-Internal-Token": self.gateway_internal_token,
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Gateway auth-health response must be a JSON object.")
            return payload
        except Exception as exc:
            logger.warning(
                "agent.provider_health_probe_failed agent_id=%s tool=%s error=%s",
                self.agent_id,
                probe_config.get("tool"),
                exc,
            )
            return {
                "status": "degraded",
                "healthy": False,
                "available": True,
                "provider": "google",
                "tool": probe_config.get("tool"),
                "reason": "gateway_credentials_probe_failed",
                "error": str(exc)[:300],
            }

    def _google_provider_health_probe_config(self) -> dict[str, Any] | None:
        tool = self._google_provider_tool_name()
        if not tool:
            return None
        auth_requirements = self.agent_card.get("auth_requirements")
        if not isinstance(auth_requirements, dict):
            return None
        scopes: set[str] = set()
        for requirement in auth_requirements.values():
            if not isinstance(requirement, dict):
                continue
            if str(requirement.get("provider") or "").strip().lower() != "google":
                continue
            for scope in requirement.get("scopes") or []:
                normalized = str(scope or "").strip()
                if normalized:
                    scopes.add(normalized)
        if not scopes:
            return None
        return {"provider": "google", "tool": tool, "required_scopes": sorted(scopes)}

    def _google_provider_tool_name(self) -> str | None:
        agent_id = self.agent_id.lower()
        if "gmail-agent" in agent_id:
            return "gmail"
        if "calendar-agent" in agent_id:
            return "calendar"
        if "google-docs-agent" in agent_id:
            return "docs"
        if "google-sheets-agent" in agent_id:
            return "sheets"
        return None

    def _heartbeat(self, *, healthy: bool) -> Heartbeat:
        return Heartbeat(
            agent_id=self.agent_id,
            instance_id=self.instance_id,
            healthy=healthy,
            current_load=self._active_task_count,
            max_concurrency=self.max_concurrency,
            heartbeat_ttl_sec=self.heartbeat_ttl_sec,
        )

    def _sender_allowed(self, task: TaskEnvelope) -> bool:
        intent_senders = self.intent_authorization.get(task.intent)
        if intent_senders is not None:
            return task.sender in intent_senders
        if not self.allowed_senders:
            return True
        return task.sender in self.allowed_senders

    def _build_reverse_idempotency_key(
        self,
        current_idempotency_key: str,
        intent: str,
        input_payload: dict[str, Any],
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                input_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"{current_idempotency_key}:reverse:{intent}:{digest}"

    def _load_agent_card(self, path: Path) -> dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("agent_card.yaml must decode to an object")
        agent_id = str(raw.get("agent_id") or "").strip()
        if not agent_id:
            raise ValueError("agent_card.yaml is missing agent_id")
        intents = raw.get("intents")
        if not isinstance(intents, list) or not intents:
            raise ValueError("agent_card.yaml must declare at least one intent")
        return self._enrich_agent_card(raw, base_dir=path.parent)

    @staticmethod
    def _safe_positive_int(value: Any, *, fallback: int, minimum: int = 1) -> int:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            parsed = fallback
        return max(minimum, parsed)

    def _enrich_agent_card(self, card: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
        enriched = dict(card)
        enriched_intents: list[dict[str, Any]] = []
        for raw_intent in card.get("intents", []):
            if not isinstance(raw_intent, dict):
                continue
            intent = dict(raw_intent)
            input_schema_summary = self._load_schema_summary(base_dir, intent.get("input_schema"))
            if input_schema_summary:
                intent["input_schema_summary"] = input_schema_summary
            output_schema_summary = self._load_schema_summary(base_dir, intent.get("output_schema"))
            if output_schema_summary:
                intent["output_schema_summary"] = output_schema_summary
            enriched_intents.append(intent)
        enriched["intents"] = enriched_intents
        return enriched

    def _load_schema_summary(self, base_dir: Path, schema_ref: Any) -> dict[str, Any] | None:
        schema_name = str(schema_ref or "").strip()
        if not schema_name:
            return None
        schema_path = Path(schema_name)
        if not schema_path.is_absolute():
            schema_path = (base_dir / schema_path).resolve()
        try:
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        summary: dict[str, Any] = {}
        description = str(payload.get("description") or "").strip()
        if description:
            summary["description"] = description[:240]

        schema_type = self._schema_type_label(payload)
        if schema_type:
            summary["type"] = schema_type

        required = payload.get("required")
        required_names: list[str] = []
        if isinstance(required, list):
            required_names = [
                str(name).strip()
                for name in required
                if str(name or "").strip()
            ]
        if required_names:
            summary["required"] = required_names
        required_set = set(required_names)

        properties = payload.get("properties")
        if isinstance(properties, dict) and properties:
            property_summaries: list[dict[str, Any]] = []
            for prop_name, prop_schema in list(properties.items())[:12]:
                if not isinstance(prop_schema, dict):
                    continue
                name = str(prop_name or "").strip()
                if not name:
                    continue
                item: dict[str, Any] = {"name": name}
                prop_type = self._schema_type_label(prop_schema)
                if prop_type:
                    item["type"] = prop_type
                prop_description = str(prop_schema.get("description") or "").strip()
                if prop_description:
                    item["description"] = prop_description[:160]
                enum_values = prop_schema.get("enum")
                if isinstance(enum_values, list) and enum_values:
                    item["enum"] = [str(value)[:48] for value in enum_values[:6]]
                for constraint_name in ("minimum", "maximum", "default"):
                    if constraint_name in prop_schema:
                        item[constraint_name] = prop_schema[constraint_name]
                if name in required_set:
                    item["required"] = True
                property_summaries.append(item)
            if property_summaries:
                summary["properties"] = property_summaries

        return summary or None

    def _schema_type_label(self, schema: dict[str, Any]) -> str | None:
        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            labels = [str(item).strip() for item in raw_type if str(item or "").strip()]
            return " | ".join(labels) if labels else None
        schema_type = str(raw_type or "").strip()
        if schema_type == "array":
            items = schema.get("items")
            if isinstance(items, dict):
                item_type = self._schema_type_label(items)
                if item_type:
                    return f"array<{item_type}>"
            return "array"
        if schema_type:
            return schema_type
        if isinstance(schema.get("enum"), list) and schema.get("enum"):
            return "enum"
        if isinstance(schema.get("properties"), dict):
            return "object"
        return None

    def _extract_claimed_messages(self, payload: Any) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(payload, (tuple, list)) or len(payload) < 2:
            return []
        raw_messages = payload[1]
        if not isinstance(raw_messages, list):
            return []
        messages: list[tuple[str, dict[str, Any]]] = []
        for item in raw_messages:
            if (
                isinstance(item, (tuple, list))
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], dict)
            ):
                messages.append((item[0], item[1]))
        return messages
