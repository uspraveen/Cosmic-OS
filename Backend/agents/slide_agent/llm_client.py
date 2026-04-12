"""Shared Fireworks LLM client for HTML backend calls."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

MODEL_BASE_URL: str = os.getenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference/v1").rstrip("/")
MODEL_API_KEY: str = os.getenv("MODEL_API_KEY", "")
MODEL_NAME: str = os.getenv("MODEL_NAME", "accounts/fireworks/models/qwen3p6-plus")
MODEL_TIMEOUT_SEC: int = int(os.getenv("MODEL_TIMEOUT_SEC", "300"))
MODEL_HTTP_RETRIES: int = int(os.getenv("MODEL_HTTP_RETRIES", "3"))
MODEL_MAX_TOKENS: int = int(os.getenv("HTML_MODEL_MAX_TOKENS", "4096"))

logger = logging.getLogger(__name__)


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
    """Call Fireworks chat completions and return message content."""
    payload: dict[str, Any] = {
        "model": model or MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "max_tokens": max_tokens or MODEL_MAX_TOKENS,
    }
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
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort

    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{MODEL_BASE_URL}/chat/completions"

    last_exc: Exception | None = None
    for attempt in range(MODEL_HTTP_RETRIES):
        try:
            with httpx.Client(timeout=MODEL_TIMEOUT_SEC) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM API {resp.status_code}: {resp.text[:500]}")
            data = resp.json()
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
            max_tokens=max_tokens,
            response_schema=response_schema,
            force_json_object=response_schema is None,
            reasoning_effort=reasoning_effort,
        )
        return parse_json_response(retry_raw)
