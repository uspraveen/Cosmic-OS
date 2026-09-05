"""Shared OpenAI-compatible LLM client (Fireworks Qwen or OpenAI GPT-5.6) for slide calls."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from shared import infer_model_provider, is_openai_gpt5_chat_model, normalized_reasoning_effort
from shared.usage import UsageEvent, utcnow_iso

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

MODEL_BASE_URL: str = os.getenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference/v1").rstrip("/")
MODEL_API_KEY: str = os.getenv("MODEL_API_KEY", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "accounts/fireworks/models/glm-5p3-flash")

logger = logging.getLogger(__name__)

# (model, effort) pairs this provider has already rejected. Thread-safe: the
# design/validation stages fan LLM calls out across worker threads.
_EFFORT_REJECTIONS: set[tuple[str, str]] = set()
_EFFORT_REJECTIONS_LOCK = threading.Lock()


def _effort_known_rejected(model: str, effort: Any) -> bool:
    with _EFFORT_REJECTIONS_LOCK:
        return (model, str(effort)) in _EFFORT_REJECTIONS


def _memoize_effort_rejection(model: str, effort: Any) -> None:
    with _EFFORT_REJECTIONS_LOCK:
        _EFFORT_REJECTIONS.add((model, str(effort)))


def env_int(name: str, default: int) -> int:
    """Read an integer env var without crashing on "120.0"-style values.

    Deployment env files routinely write numeric settings as floats ("120.0");
    bare int() at import time turned that into a module-load crash, which took
    down every pipeline in the agent while the service itself stayed healthy.
    """
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("llm_client: ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


MODEL_TIMEOUT_SEC: int = env_int("MODEL_TIMEOUT_SEC", 300)
MODEL_HTTP_RETRIES: int = env_int("MODEL_HTTP_RETRIES", 3)
MODEL_MAX_TOKENS: int = env_int("HTML_MODEL_MAX_TOKENS", 4096)
MODEL_REASONING_EFFORT: str = os.getenv("MODEL_REASONING_EFFORT", "xhigh")


def _json_candidates(raw: str) -> list[str]:
    """Yield possible JSON object substrings from a noisy model response."""
    decoder = json.JSONDecoder()
    candidates: list[str] = []
    seen: set[str] = set()

    starts = [idx for idx, ch in enumerate(raw) if ch == "{"] or [0]
    for start in starts:
        snippet = raw[start:].lstrip()
        if not snippet.startswith("{"):
            continue
        try:
            obj, end = decoder.raw_decode(snippet)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidate = snippet[:end]
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def _build_payload(
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    model: str,
    max_tokens: int,
    response_schema: dict[str, Any] | None,
    force_json_object: bool,
    reasoning_effort: str | bool | int | None,
) -> dict[str, Any]:
    """Build the chat-completions body for the resolved model's family.

    GPT-5-family models reject temperature/max_tokens and take
    max_completion_tokens plus reasoning_effort; other (Fireworks/Qwen) models
    keep the original shape untouched.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if is_openai_gpt5_chat_model(model):
        payload["max_completion_tokens"] = max_tokens
        # Bool/int efforts are Qwen-style toggles; only valid level strings apply.
        requested = reasoning_effort if isinstance(reasoning_effort, str) else None
        if not (requested or "").strip():
            requested = MODEL_REASONING_EFFORT
        effort = normalized_reasoning_effort(model, requested)
        if effort:
            payload["reasoning_effort"] = effort
    else:
        payload["temperature"] = temperature
        payload["max_tokens"] = max_tokens
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "schema": response_schema,
            },
        }
    elif force_json_object:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _post_usage(*, model: str, data: dict[str, Any], placed_at: str, latency_ms: int) -> None:
    """Best-effort usage event to the Gateway ledger; never raises."""
    gateway_url = os.getenv("GATEWAY_URL", "").rstrip("/")
    gateway_token = os.getenv("GATEWAY_INTERNAL_TOKEN", "")
    if not gateway_url or not gateway_token:
        return
    try:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        event = UsageEvent(
            llm_call_id=f"slide_llm_{uuid4().hex[:16]}",
            source_component="cosmic/slide-agent:1.0.0",
            route="slides",
            operation="slide.llm",
            usage_kind="chat_completion",
            provider=infer_model_provider(MODEL_BASE_URL, model),
            model=model,
            prompt_tokens=max(0, prompt_tokens),
            completion_tokens=max(0, completion_tokens),
            total_tokens=max(0, prompt_tokens) + max(0, completion_tokens),
            latency_ms=max(0, latency_ms),
            llm_call_placed_at=placed_at,
        )
        with httpx.Client(timeout=5.0) as client:
            client.post(
                f"{gateway_url}/internal/usage/log",
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Token": gateway_token,
                },
                json=event.model_dump(mode="json"),
            )
    except Exception as exc:
        logger.debug("slide usage post failed: %s", exc)


def call_llm(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    model: str | None = None,
    max_tokens: int | None = None,
    response_schema: dict[str, Any] | None = None,
    force_json_object: bool = False,
    reasoning_effort: str | bool | int | None = None,
) -> str:
    """Call the configured chat-completions model and return message content."""
    resolved_model = model or MODEL_NAME
    # "none" keeps non-thinking models cheap, but thinking-only models (GLM-5.3)
    # reject it outright. If the provider says so, drop the field and retry with
    # its default effort instead of failing the call. The rejection is memoized
    # per provider+effort so later calls skip the doomed attempt entirely
    # instead of paying one wasted round-trip each.
    adaptive_effort = reasoning_effort
    if adaptive_effort is not None and _effort_known_rejected(resolved_model, adaptive_effort):
        adaptive_effort = None

    # Thinking models spend the SAME max_tokens budget on reasoning and on the
    # answer, so a small budget truncates the answer mid-JSON ("finish_reason":
    # "length"). On truncation, retry with a doubled budget instead of handing
    # back a cut-off response for the caller's JSON parser to choke on.
    base_max_tokens = max_tokens or MODEL_MAX_TOKENS
    adaptive_max_tokens = base_max_tokens

    last_exc: Exception | None = None
    for attempt in range(MODEL_HTTP_RETRIES):
        payload = _build_payload(
            messages,
            temperature=temperature,
            model=resolved_model,
            max_tokens=adaptive_max_tokens,
            response_schema=response_schema,
            force_json_object=force_json_object,
            reasoning_effort=adaptive_effort,
        )

        headers = {
            "Authorization": f"Bearer {MODEL_API_KEY}",
            "Content-Type": "application/json",
        }
        url = f"{MODEL_BASE_URL}/chat/completions"

        placed_at = utcnow_iso()
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=MODEL_TIMEOUT_SEC) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                error_text = resp.text[:500]
                if (
                    adaptive_effort is not None
                    and "reasoning_effort" in error_text
                    and attempt + 1 < MODEL_HTTP_RETRIES
                ):
                    logger.warning(
                        "llm_client: provider rejected reasoning_effort=%r; retrying without it",
                        adaptive_effort,
                    )
                    _memoize_effort_rejection(resolved_model, adaptive_effort)
                    adaptive_effort = None
                    continue
                raise RuntimeError(f"LLM API {resp.status_code}: {error_text}")
            data = resp.json()
            finish_reason = (data.get("choices") or [{}])[0].get("finish_reason")
            if finish_reason == "length" and attempt + 1 < MODEL_HTTP_RETRIES and adaptive_max_tokens < 65536:
                adaptive_max_tokens = min(adaptive_max_tokens * 2, 65536)
                logger.warning(
                    "llm_client: response truncated (finish_reason=length); retrying with max_tokens=%s",
                    adaptive_max_tokens,
                )
                continue
            _post_usage(
                model=resolved_model,
                data=data,
                placed_at=placed_at,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            msg = (data.get("choices") or [{}])[0].get("message", {})
            content = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content", "") or ""
            if not content and reasoning:
                content = reasoning
            if not content:
                raise ValueError("Empty LLM response")
            logger.debug("llm: content=%d reasoning=%d", len(content), len(reasoning))
            return content.strip()
        except (httpx.TimeoutException, httpx.TransportError, RuntimeError, ValueError) as exc:
            last_exc = exc
            if attempt + 1 >= MODEL_HTTP_RETRIES:
                break
            wait = min(20, 2 ** attempt)
            logger.warning("llm call failed (%s), retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"LLM call failed after retries: {last_exc}")


def parse_json_response(raw: str) -> dict[str, Any]:
    """Parse JSON from raw LLM text with robust cleanup and extraction."""
    if not raw:
        raise ValueError("Empty response")

    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()

    for candidate in [raw, *_json_candidates(raw)]:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                parsed = json.loads(fixed)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

    if "{" in raw and "}" in raw:
        blob = raw[raw.index("{"): raw.rindex("}") + 1]
        blob = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No valid JSON found in response: {raw[:250]!r}")


def call_llm_json(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.2,
    model: str | None = None,
    max_tokens: int | None = None,
    response_schema: dict[str, Any] | None = None,
    reasoning_effort: str | bool | int | None = "none",
) -> dict[str, Any]:
    """Call the model and parse a JSON object from the response."""
    raw = call_llm(
        messages,
        temperature=temperature,
        model=model,
        max_tokens=max_tokens,
        response_schema=response_schema,
        force_json_object=response_schema is None,
        reasoning_effort=reasoning_effort,
    )
    try:
        return parse_json_response(raw)
    except ValueError as exc:
        logger.warning("structured parse failed, retrying with strict JSON reminder: %s", exc)
        retry_messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    "Return ONLY one valid JSON object matching the requested schema. "
                    "No prose, no analysis, no markdown fences."
                ),
            },
        ]
        retry_raw = call_llm(
            retry_messages,
            temperature=min(temperature, 0.1),
            model=model,
            # A parse failure often means the previous answer was truncated;
            # give the retry a doubled budget instead of the same ceiling.
            max_tokens=min((max_tokens or MODEL_MAX_TOKENS) * 2, 65536),
            response_schema=response_schema,
            force_json_object=response_schema is None,
            reasoning_effort=reasoning_effort,
        )
        return parse_json_response(retry_raw)
