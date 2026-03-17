from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_specs import build_model_key, get_model_spec, lookup_model_spec


@dataclass(frozen=True, slots=True)
class MeteredCall:
    llm_call_id: str
    llm_call_placed_at: str
    started_perf_counter: float


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_call_id: str
    user_id: str | None = None
    source_component: str
    source_id: str | None = None
    task_id: str | None = None
    plan_id: str | None = None
    parent_task_id: str | None = None
    session_id: str | None = None
    route: str | None = None
    operation: str
    usage_kind: str
    provider: str
    model: str
    request_id: str | None = None
    provider_request_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost_usd: float | None = None
    latency_ms: int | None = None
    success: bool = True
    error_code: str | None = None
    metadata_json: Any | None = None
    llm_call_placed_at: str

    @field_validator(
        "llm_call_id",
        "source_component",
        "operation",
        "usage_kind",
        "provider",
        "model",
        mode="before",
    )
    @classmethod
    def _require_text(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value must be non-empty")
        return normalized

    @field_validator(
        "user_id",
        "source_id",
        "task_id",
        "plan_id",
        "parent_task_id",
        "session_id",
        "route",
        "request_id",
        "provider_request_id",
        "error_code",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator(
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
        mode="before",
    )
    @classmethod
    def _normalize_token_int(cls, value: Any) -> int:
        if value is None or value == "":
            return 0
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _normalize_latency(cls, value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return None

    @field_validator("estimated_cost_usd", mode="before")
    @classmethod
    def _normalize_cost(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @field_validator("llm_call_placed_at", mode="before")
    @classmethod
    def _normalize_placed_at(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("llm_call_placed_at is required")
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("llm_call_placed_at must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def begin_metered_call(*, prefix: str = "call") -> MeteredCall:
    normalized_prefix = str(prefix or "call").strip().lower() or "call"
    return MeteredCall(
        llm_call_id=f"{normalized_prefix}_{uuid4().hex}",
        llm_call_placed_at=utcnow_iso(),
        started_perf_counter=time.perf_counter(),
    )


def normalize_usage(model_key: str, raw_usage: Any) -> dict[str, int]:
    mappings = {
        "prompt_tokens": ["prompt_tokens", "input_tokens"],
        "completion_tokens": ["completion_tokens", "output_tokens"],
        "total_tokens": ["total_tokens"],
        "cached_tokens": ["cached_tokens", "cache_read_input_tokens"],
        "reasoning_tokens": ["reasoning_tokens"],
    }
    spec = get_model_spec(model_key)
    if spec is not None and spec.token_field_map:
        mappings = {
            key: (value or mappings.get(key, []))
            for key, value in spec.token_field_map.items()
        } | {
            key: mappings.get(key, [])
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")
            if key not in (spec.token_field_map or {})
        }

    prompt_tokens = _read_first_int(raw_usage, mappings.get("prompt_tokens", []))
    completion_tokens = _read_first_int(raw_usage, mappings.get("completion_tokens", []))
    total_tokens = _read_first_int(raw_usage, mappings.get("total_tokens", []))
    cached_tokens = _read_first_int(raw_usage, mappings.get("cached_tokens", []))
    reasoning_tokens = _read_first_int(raw_usage, mappings.get("reasoning_tokens", []))

    if total_tokens == 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    if cached_tokens > prompt_tokens:
        cached_tokens = prompt_tokens
    if reasoning_tokens > completion_tokens:
        reasoning_tokens = completion_tokens

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
    }


def build_usage_event(
    *,
    metered_call: MeteredCall,
    source_component: str,
    operation: str,
    model_key: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    usage_kind: str | None = None,
    source_id: str | None = None,
    task_id: str | None = None,
    plan_id: str | None = None,
    parent_task_id: str | None = None,
    session_id: str | None = None,
    route: str | None = None,
    request_id: str | None = None,
    provider_request_id: str | None = None,
    user_id: str | None = None,
    raw_usage: Any = None,
    success: bool = True,
    error_code: str | None = None,
    latency_ms: int | None = None,
    estimated_cost_usd: float | None = None,
    metadata_json: Any | None = None,
) -> UsageEvent:
    resolved_provider = str(provider or "").strip()
    resolved_model = str(model or "").strip()
    resolved_usage_kind = str(usage_kind or "").strip()
    resolved_model_key = str(model_key or "").strip()
    if not resolved_model_key and resolved_provider and resolved_model:
        resolved_model_key = build_model_key(resolved_provider, resolved_model)

    if resolved_model_key:
        spec = get_model_spec(resolved_model_key)
        if spec is not None:
            resolved_provider = resolved_provider or spec.provider
            resolved_model = resolved_model or spec.model
            resolved_usage_kind = resolved_usage_kind or spec.usage_kind

    if not resolved_provider or not resolved_model:
        if resolved_model_key and ":" in resolved_model_key:
            guessed_provider, guessed_model = resolved_model_key.split(":", 1)
            resolved_provider = resolved_provider or guessed_provider
            resolved_model = resolved_model or guessed_model
    if not resolved_usage_kind:
        resolved_usage_kind = "other"

    normalized_usage = normalize_usage(
        resolved_model_key or build_model_key(resolved_provider, resolved_model),
        raw_usage,
    )
    if latency_ms is None:
        latency_ms = max(0, int((time.perf_counter() - metered_call.started_perf_counter) * 1000))
    if estimated_cost_usd is None:
        estimated_cost_usd = extract_provider_cost_usd(raw_usage)

    combined_metadata = _merge_metadata(
        metadata_json,
        {
            "raw_usage": serialize_usage_metadata(raw_usage),
        },
    )

    return UsageEvent(
        llm_call_id=metered_call.llm_call_id,
        user_id=user_id,
        source_component=source_component,
        source_id=source_id,
        task_id=task_id,
        plan_id=plan_id,
        parent_task_id=parent_task_id,
        session_id=session_id,
        route=route,
        operation=operation,
        usage_kind=resolved_usage_kind,
        provider=resolved_provider,
        model=resolved_model,
        request_id=request_id,
        provider_request_id=provider_request_id,
        prompt_tokens=normalized_usage["prompt_tokens"],
        completion_tokens=normalized_usage["completion_tokens"],
        total_tokens=normalized_usage["total_tokens"],
        cached_tokens=normalized_usage["cached_tokens"],
        reasoning_tokens=normalized_usage["reasoning_tokens"],
        estimated_cost_usd=estimated_cost_usd,
        latency_ms=latency_ms,
        success=success,
        error_code=error_code if not success else None,
        metadata_json=combined_metadata,
        llm_call_placed_at=metered_call.llm_call_placed_at,
    )


async def post_usage_event(
    *,
    client: httpx.AsyncClient,
    gateway_url: str,
    internal_token: str,
    event: UsageEvent | dict[str, Any],
    timeout_sec: float = 5.0,
    max_attempts: int = 3,
    base_delay_sec: float = 0.25,
) -> bool:
    if not gateway_url.strip() or not internal_token.strip():
        return False
    payload = event.model_dump(mode="json") if isinstance(event, UsageEvent) else dict(event)
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Token": internal_token,
    }
    url = gateway_url.rstrip("/") + "/internal/usage/log"
    for attempt in range(max(1, max_attempts)):
        try:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=httpx.Timeout(timeout_sec, connect=min(timeout_sec, 2.5)),
            )
            if response.status_code in {200, 201, 202}:
                return True
            response.raise_for_status()
            return True
        except Exception:
            if attempt >= max(1, max_attempts) - 1:
                return False
            await asyncio.sleep(base_delay_sec * (2**attempt))
    return False


def extract_provider_cost_usd(raw_usage: Any) -> float | None:
    for candidate in (
        "cost.total_cost",
        "cost.total_cost_usd",
        "cost_usd",
        "total_cost_usd",
        "total_cost",
    ):
        value = _read_path(raw_usage, candidate)
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def serialize_usage_metadata(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): serialize_usage_metadata(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [serialize_usage_metadata(item) for item in value]
    if hasattr(value, "model_dump"):
        return serialize_usage_metadata(value.model_dump(mode="json"))
    if hasattr(value, "dict") and callable(value.dict):
        return serialize_usage_metadata(value.dict())
    if hasattr(value, "__dict__"):
        return serialize_usage_metadata(vars(value))
    return str(value)


def _merge_metadata(primary: Any | None, secondary: dict[str, Any]) -> Any | None:
    secondary_clean = {key: value for key, value in secondary.items() if value is not None}
    if primary is None:
        return secondary_clean or None
    if not secondary_clean:
        return serialize_usage_metadata(primary)
    primary_serialized = serialize_usage_metadata(primary)
    if isinstance(primary_serialized, dict):
        return {
            **primary_serialized,
            **secondary_clean,
        }
    return {
        "extra": primary_serialized,
        **secondary_clean,
    }


def _read_first_int(raw_usage: Any, candidates: list[str]) -> int:
    for candidate in candidates:
        value = _read_path(raw_usage, candidate)
        if value is None or value == "":
            continue
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return 0


def _read_path(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        part = part.strip()
        if not part:
            return None
        if isinstance(current, dict):
            current = current.get(part)
            continue
        current = getattr(current, part, None)
    return current
