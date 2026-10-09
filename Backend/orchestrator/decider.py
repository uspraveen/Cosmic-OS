"""The orchestrator's yes/no decider question, with Perplexity retried and
OpenAI's Decisions API as the fallback.

Perplexity's pplx-decider stays the primary: it won the 2026-10-09 bake-off
on hand-labeled Cosmic browser states (submit check 91% vs 86% for OpenAI's
gpt-6-luna, AUROC 0.99 vs 0.90). But it allows 10 requests/second per
organization, shared by every Cosmic run, while OpenAI's endpoint takes
5,000 requests/minute. So: up to `attempts` Perplexity calls, retrying
429/5xx and transport errors (waiting for the rate-limit window, bounded),
then one OpenAI call. A timeout or any other status skips straight to the
fallback — the same request would not fare better a second time.

The browser agent's fast path does the same for its step and type-value
questions (cosmic-browser-use/decisions_client.py).
"""
from __future__ import annotations

import asyncio
import base64
import random
import time
from typing import Any, Awaitable, Callable

import httpx

PPLX_DECISIONS_URL = "https://api.perplexity.ai/v1/decisions"
PPLX_DECIDER_MODEL = "pplx-decider-v1.1-27b"
OPENAI_DECISIONS_URL = "https://api.openai.com/v1/decisions"
OPENAI_DECIDER_MODEL = "gpt-6-luna"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 529})


def image_data_url(screenshot_b64: str | None) -> str | None:
    """A data URL whose MIME type matches the bytes, or None.

    Step captures are JPEG whatever their label says; OpenAI checks the
    declared type against the bytes, so it is re-derived here."""
    payload = str(screenshot_b64 or "").strip()
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[1] if "," in payload else ""
    if len(payload) < 64:
        return None
    try:
        head = base64.b64decode(payload[:24] + "=" * (-len(payload[:24]) % 4))
    except (ValueError, TypeError):
        return None
    if head.startswith(b"\x89PNG"):
        mime = "image/png"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    return f"data:{mime};base64,{payload}"


def _wait(response: httpx.Response, attempt: int, cap: float) -> float:
    wait = None
    try:
        wait = float(response.headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    if wait is None:
        try:
            wait = float(response.headers.get("x-ratelimit-reset")) - time.time()
        except (TypeError, ValueError):
            pass
    if wait is None or wait <= 0:
        wait = 0.25 * (2**attempt)
    return min(max(wait, 0.1), cap) + random.uniform(0, 0.1)


async def ask_yes_no(
    client: httpx.AsyncClient,
    *,
    name: str,
    instructions: str,
    texts: list[str],
    image_url: str | None,
    pplx_key: str,
    openai_key: str,
    timeout_sec: float,
    attempts: int = 3,
    max_wait_sec: float = 1.0,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> tuple[float | None, str, str]:
    """(probability the answer is yes, provider that answered, why the
    primary did not answer). The probability is None when nobody answered."""
    sleep = sleep or asyncio.sleep
    error = ""
    if pplx_key:
        state: list[Any] = list(texts)
        if image_url:
            state.append({"type": "image_url", "image_url": {"url": image_url}})
        body = {
            "model": PPLX_DECIDER_MODEL,
            "state": state,
            "questions": {name: {"type": "noul", "instructions": instructions}},
        }
        for attempt in range(max(1, attempts)):
            last = attempt + 1 >= max(1, attempts)
            try:
                response = await client.post(
                    PPLX_DECISIONS_URL,
                    headers={"Authorization": f"Bearer {pplx_key}"},
                    json=body,
                    timeout=timeout_sec,
                )
            except httpx.TimeoutException:
                error = "timeout"
                break
            except httpx.HTTPError as exc:
                error = f"transport: {type(exc).__name__}"
                if last:
                    break
                await sleep(min(0.25 * (2**attempt), max_wait_sec))
                continue
            if response.status_code < 400:
                try:
                    value = float(((response.json() or {}).get("answers") or {}).get(name, {}).get("noul"))
                except (ValueError, TypeError, AttributeError):
                    error = "unreadable answer"
                    break
                if value == value:  # not NaN
                    return max(0.0, min(1.0, value)), "pplx", ""
                error = "NaN answer"
                break
            error = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUSES or last:
                break
            await sleep(_wait(response, attempt, max_wait_sec))
    else:
        error = "no Perplexity key"

    if not openai_key:
        return None, "", error
    content: list[dict[str, Any]] = [{"type": "input_text", "text": text} for text in texts]
    if image_url:
        content.append({"type": "input_image", "image_url": image_url})
    request = {
        "model": OPENAI_DECIDER_MODEL,
        "input": [{"role": "user", "content": content}],
        "questions": [{"type": "predicate", "name": name, "instructions": instructions}],
    }
    for attempt in range(2):
        try:
            response = await client.post(
                OPENAI_DECISIONS_URL,
                headers={"Authorization": f"Bearer {openai_key}"},
                json=request,
                timeout=timeout_sec,
            )
        except httpx.HTTPError as exc:
            return None, "", f"{error}; fallback transport: {type(exc).__name__}"
        if response.status_code in {429, 500, 502, 503} and attempt == 0:
            await sleep(0.25)
            continue
        if response.status_code >= 400:
            return None, "", f"{error}; fallback HTTP {response.status_code}"
        try:
            answers = (response.json() or {}).get("answers") or []
            answer = next(a for a in answers if isinstance(a, dict) and a.get("name") == name)
            value = float(answer.get("probability"))
        except (StopIteration, ValueError, TypeError, AttributeError):
            return None, "", f"{error}; fallback gave no answer"
        if value != value:
            return None, "", f"{error}; fallback NaN"
        return max(0.0, min(1.0, value)), "openai", error
    return None, "", f"{error}; fallback busy"
