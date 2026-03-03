#!/usr/bin/env python3
"""
COSMIC model router classifier service.

Production runtime:
- Standalone FastAPI microservice behind the Gateway
- Internal-only HTTP surface: /health, /health/ready, /classify
- Three possible routes only: opus, gemini, perplexity

Developer utilities remain available for local validation:
- Single query:
    python model_router.py "your question"
- Timed test:
    python model_router.py --test "your question"
- Test suite:
    python model_router.py --test
- Server mode:
    python model_router.py --server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).with_name("model_router.env"))
load_dotenv()


# ============================================================================
# Configuration
# ============================================================================


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "openai/gpt-oss-20b")
GROQ_API_BASE = "https://api.groq.com/openai/v1"
SERVER_HOST = os.getenv("MODEL_ROUTER_HOST", "0.0.0.0")
SERVER_PORT = env_int("MODEL_ROUTER_PORT", 8742)
HTTP2_ENABLED = env_bool("HTTP2_ENABLED", True)
CONNECTION_POOL_SIZE = max(1, env_int("CONNECTION_POOL_SIZE", 10))
KEEPALIVE_EXPIRY = max(1, env_int("KEEPALIVE_EXPIRY", 30))
HTTP_TIMEOUT_SEC = max(1.0, env_float("MODEL_ROUTER_HTTP_TIMEOUT_SEC", 30.0))
CONNECT_TIMEOUT_SEC = max(1.0, env_float("MODEL_ROUTER_CONNECT_TIMEOUT_SEC", 10.0))
DEFAULT_MAX_COMPLETION_TOKENS = max(1, env_int("MODEL_ROUTER_DEFAULT_MAX_COMPLETION_TOKENS", 380))
LOW_CONFIDENCE_THRESHOLD = min(1.0, max(0.0, env_float("MODEL_ROUTER_LOW_CONFIDENCE_THRESHOLD", 0.5)))
LOG_LEVEL = os.getenv("MODEL_ROUTER_LOG_LEVEL", "INFO").upper()

ALLOWED_ROUTES = {"opus", "gemini", "perplexity"}
ALLOWED_CONTEXT_ROLES = {"user", "assistant"}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cosmic.model_router")


# ============================================================================
# FastAPI request models
# ============================================================================


class ConversationContextMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)
    route: Optional[str] = None


class ClassifyRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_context: List[ConversationContextMessage] = Field(default_factory=list)
    max_completion_tokens: int = Field(default=DEFAULT_MAX_COMPLETION_TOKENS, ge=1, le=4096)


# ============================================================================
# Global state
# ============================================================================


_http_client: Optional[httpx.AsyncClient] = None
_connection_warmed: bool = False
_warmup_latency_ms: Optional[float] = None


# ============================================================================
# Helpers
# ============================================================================


def now() -> float:
    return time.perf_counter()


def strip_code_fences(raw: str) -> str:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = strip_code_fences(raw)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None

    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def normalize_route(route: Any) -> str:
    if not isinstance(route, str):
        return "gemini"
    normalized = route.strip().lower()
    return normalized if normalized in ALLOWED_ROUTES else "gemini"


def optional_route(route: Any) -> Optional[str]:
    if not isinstance(route, str):
        return None
    normalized = route.strip().lower()
    return normalized if normalized in ALLOWED_ROUTES else None


def safe_float(value: Any, default: float = LOW_CONFIDENCE_THRESHOLD) -> float:
    try:
        parsed = float(value)
        if parsed < 0.0:
            return 0.0
        if parsed > 1.0:
            return 1.0
        return parsed
    except Exception:
        return default


def normalize_signals(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []

    signals: List[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in signals:
            signals.append(text)
        if len(signals) >= 6:
            break
    return signals


def default_classifier_output(signal: str) -> Dict[str, Any]:
    return {
        "route": "gemini",
        "needs_latest": False,
        "needs_citations": False,
        "is_task": False,
        "is_continuation": False,
        "confidence": 0.0,
        "signals": [signal],
    }


def normalize_context_messages(conversation_context: Optional[Sequence[Any]]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    if not conversation_context:
        return normalized

    for item in conversation_context:
        payload: Dict[str, Any]
        if isinstance(item, BaseModel):
            payload = item.dict()
        elif isinstance(item, dict):
            payload = item
        else:
            continue

        role = payload.get("role")
        content = payload.get("content")
        route = optional_route(payload.get("route"))
        if role not in ALLOWED_CONTEXT_ROLES or not isinstance(content, str):
            continue

        text = content.strip()
        if not text:
            continue

        # Preserve the last-model hint from Gateway session history when available.
        if route and role == "assistant":
            text = f"[assistant_route={route}] {text}"

        normalized.append({"role": role, "content": text})

    return normalized


def build_messages(
    user_text: str,
    conversation_context: Optional[Sequence[Any]] = None,
) -> List[Dict[str, str]]:
    system = (
        "You are a STRICT JSON-only classifier for the COSMIC model router.\n"
        "Return exactly one JSON object and nothing else.\n\n"
        "Decide which backend should answer:\n"
        '  - "opus": tasks, continuations, ambiguous inputs, tool-use, coding, drafting, execution, workflow help.\n'
        '  - "perplexity": time-sensitive or verification-heavy questions that need current information or citations.\n'
        '  - "gemini": timeless/general explanations, brainstorming, concepts, theory, non-time-sensitive knowledge.\n\n'
        "Output schema (keys must match):\n"
        "{\n"
        '  "route": "opus|perplexity|gemini",\n'
        '  "needs_latest": true|false,\n'
        '  "needs_citations": true|false,\n'
        '  "is_task": true|false,\n'
        '  "is_continuation": true|false,\n'
        '  "confidence": 0.0-1.0,\n'
        '  "signals": ["max 6 short strings"]\n'
        "}\n\n"
        "Classification guidance:\n"
        "- is_task=true if the user asks the assistant to do, make, create, fix, send, build, run, plan, draft, or operate something.\n"
        "- is_continuation=true for ambiguous follow-ups or conversation carry-ons such as 'go on', 'why?', 'ok', 'continue', progress checks, or replies that clearly depend on prior context.\n"
        "- needs_latest=true if the answer could change over time: current events, news, releases, prices, laws, schedules, product support, office holders, or anything 'latest/current/today/this week'.\n"
        "- needs_citations=true when the user likely expects source-grounded verification. Time-sensitive questions usually imply this.\n"
        "- If prior assistant messages are annotated with [assistant_route=...], use that as context when deciding whether the new input is a continuation.\n\n"
        "Hard routing rules (must follow):\n"
        "- If is_task is true, route MUST be opus.\n"
        "- Else if is_continuation is true, route MUST be opus.\n"
        "- Else if needs_latest OR needs_citations is true, route MUST be perplexity.\n"
        f"- Else if confidence < {LOW_CONFIDENCE_THRESHOLD:.2f}, route MUST be opus.\n"
        "- Else route MUST be gemini.\n"
        "- There is NO unknown route.\n"
    )

    context_messages = normalize_context_messages(conversation_context)
    return [
        {"role": "system", "content": system},
        *context_messages,
        {"role": "user", "content": user_text},
    ]


def enforce_rules(parsed: Dict[str, Any]) -> Dict[str, Any]:
    signals = normalize_signals(parsed.get("signals"))
    confidence = safe_float(parsed.get("confidence"), LOW_CONFIDENCE_THRESHOLD)
    raw_route = parsed.get("route")
    if raw_route is not None and optional_route(raw_route) is None:
        confidence = 0.0
        if "unsupported_route" not in signals:
            signals.append("unsupported_route")

    out = {
        "route": normalize_route(parsed.get("route")),
        "needs_latest": bool(parsed.get("needs_latest")),
        "needs_citations": bool(parsed.get("needs_citations")),
        "is_task": bool(parsed.get("is_task")),
        "is_continuation": bool(parsed.get("is_continuation")),
        "confidence": confidence,
        "signals": signals,
    }

    # Current-information queries should be treated as source-grounded by default.
    if out["needs_latest"] and not out["is_task"]:
        out["needs_citations"] = True

    if out["is_task"]:
        out["route"] = "opus"
    elif out["is_continuation"]:
        out["route"] = "opus"
    elif out["needs_latest"] or out["needs_citations"]:
        out["route"] = "perplexity"
    elif out["confidence"] < LOW_CONFIDENCE_THRESHOLD:
        out["route"] = "opus"
    else:
        out["route"] = "gemini"

    return out


def emit(obj: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ============================================================================
# Classifier calls
# ============================================================================


def classify_fast_cli(
    user_text: str,
    reasoning_effort: str,
    max_completion_tokens: int,
    conversation_context: Optional[Sequence[Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    completion = client.chat.completions.create(
        model=CLASSIFIER_MODEL,
        messages=build_messages(user_text, conversation_context),
        temperature=0.0,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        stream=False,
        stop=None,
    )
    raw = (completion.choices[0].message.content or "").strip()
    parsed = extract_json_object(raw) or default_classifier_output("parse_failed")
    return enforce_rules(parsed), raw


def classify_with_timing_cli(
    user_text: str,
    reasoning_effort: str,
    max_completion_tokens: int,
    conversation_context: Optional[Sequence[Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, float], str]:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    start = now()
    first_token: Optional[float] = None
    parts: List[str] = []

    completion = client.chat.completions.create(
        model=CLASSIFIER_MODEL,
        messages=build_messages(user_text, conversation_context),
        temperature=0.0,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        stream=True,
        stop=None,
    )

    for chunk in completion:
        if first_token is None:
            first_token = now()
        parts.append(chunk.choices[0].delta.content or "")

    end = now()
    if first_token is None:
        first_token = end

    raw = "".join(parts).strip()
    parsed = extract_json_object(raw) or default_classifier_output("parse_failed")
    classification = enforce_rules(parsed)
    metrics = {
        "ttft_ms": (first_token - start) * 1000,
        "rtt_ms": (end - start) * 1000,
        "estimated_network_latency_ms": (first_token - start) * 1000,
    }
    return classification, metrics, raw


def create_http2_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=HTTP2_ENABLED,
        limits=httpx.Limits(
            max_connections=CONNECTION_POOL_SIZE,
            max_keepalive_connections=CONNECTION_POOL_SIZE,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        ),
        timeout=httpx.Timeout(HTTP_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )


async def prewarm_connection() -> float:
    global _http_client, _connection_warmed, _warmup_latency_ms

    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY is not configured; model router readiness will remain false.")
        _connection_warmed = False
        _warmup_latency_ms = None
        return -1

    if _http_client is None:
        return -1

    start = now()
    try:
        response = await _http_client.post(
            f"{GROQ_API_BASE}/chat/completions",
            json={
                "model": CLASSIFIER_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "temperature": 0.0,
            },
        )
        response.raise_for_status()
        _connection_warmed = True
        _warmup_latency_ms = (now() - start) * 1000
        logger.info(
            "Model Router connection warmed in %.1fms (http2=%s)",
            _warmup_latency_ms,
            HTTP2_ENABLED,
        )
        return _warmup_latency_ms
    except Exception as exc:  # pragma: no cover - network failure path
        _connection_warmed = False
        _warmup_latency_ms = -1
        logger.warning("Model Router warmup failed: %s", exc)
        return -1


async def classify_async(
    user_text: str,
    conversation_context: Optional[Sequence[Any]] = None,
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
) -> Tuple[Dict[str, Any], Dict[str, float], str]:
    global _http_client

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")
    if _http_client is None:
        raise RuntimeError("HTTP client is not initialized")

    start = now()
    response = await _http_client.post(
        f"{GROQ_API_BASE}/chat/completions",
        json={
            "model": CLASSIFIER_MODEL,
            "messages": build_messages(user_text, conversation_context),
            "temperature": 0.0,
            "max_tokens": max_completion_tokens,
        },
    )
    response.raise_for_status()

    end = now()
    result = response.json()
    raw = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    parsed = extract_json_object(raw) or default_classifier_output("parse_failed")
    classification = enforce_rules(parsed)
    metrics = {
        "rtt_ms": (end - start) * 1000,
        "connection_warmed": _connection_warmed,
        "http2_enabled": HTTP2_ENABLED,
    }
    return classification, metrics, raw


def readiness_payload() -> Tuple[bool, Dict[str, Any]]:
    reasons: List[str] = []
    if not GROQ_API_KEY:
        reasons.append("missing_groq_api_key")
    if _http_client is None:
        reasons.append("http_client_not_initialized")

    ready = not reasons
    return ready, {
        "status": "ready" if ready else "not_ready",
        "reasons": reasons,
        "http2_enabled": HTTP2_ENABLED,
        "connection_warmed": _connection_warmed,
        "warmup_latency_ms": _warmup_latency_ms,
        "classifier_model": CLASSIFIER_MODEL,
    }


# ============================================================================
# FastAPI app
# ============================================================================


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        global _http_client
        logger.info("Starting Model Router (http2=%s)", HTTP2_ENABLED)
        _http_client = create_http2_client()
        await prewarm_connection()
        yield
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None
        logger.info("Stopped Model Router")

    app = FastAPI(
        title="COSMIC Model Router",
        description="Internal classifier service for Gateway routing decisions",
        version="2.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "healthy",
            "http2_enabled": HTTP2_ENABLED,
            "connection_warmed": _connection_warmed,
            "warmup_latency_ms": _warmup_latency_ms,
            "classifier_model": CLASSIFIER_MODEL,
        }

    @app.get("/health/ready")
    async def health_ready() -> JSONResponse:
        ready, payload = readiness_payload()
        status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(payload, status_code=status_code)

    @app.post("/classify")
    async def classify(body: ClassifyRequest) -> Dict[str, Any]:
        try:
            classification, metrics, raw = await classify_async(
                user_text=body.query,
                conversation_context=body.conversation_context,
                max_completion_tokens=body.max_completion_tokens,
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Model Router failed to reach Groq API",
            ) from exc

        logger.info(
            'route=%s rtt_ms=%.1f query="%s"',
            classification["route"],
            metrics["rtt_ms"],
            body.query[:80] + ("..." if len(body.query) > 80 else ""),
        )
        return {
            "classification": classification,
            "metrics": metrics,
            "classifier_model": CLASSIFIER_MODEL,
            "raw_classifier_output": raw,
            "timestamp_unix_ms": int(time.time() * 1000),
        }

    @app.post("/classify/batch")
    async def classify_batch(requests: List[ClassifyRequest]) -> Dict[str, Any]:
        if not requests:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty batch")

        async def _run(item: ClassifyRequest) -> Dict[str, Any]:
            classification, metrics, raw = await classify_async(
                user_text=item.query,
                conversation_context=item.conversation_context,
                max_completion_tokens=item.max_completion_tokens,
            )
            return {
                "classification": classification,
                "metrics": metrics,
                "raw_classifier_output": raw,
                "timestamp_unix_ms": int(time.time() * 1000),
            }

        try:
            results = await asyncio.gather(*[_run(item) for item in requests])
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Model Router failed to reach Groq API",
            ) from exc

        return {
            "count": len(requests),
            "classifier_model": CLASSIFIER_MODEL,
            "results": results,
            "timestamp_unix_ms": int(time.time() * 1000),
        }

    return app


app = create_app()


def run_server() -> None:
    import uvicorn

    uvicorn.run(
        "model_router:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level=LOG_LEVEL.lower(),
    )


# ============================================================================
# CLI entrypoint
# ============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="COSMIC model router.")
    parser.add_argument("query", nargs="?", help="Query to classify (optional in --test suite mode)")
    parser.add_argument("--server", action="store_true", help="Run the FastAPI classifier service")
    parser.add_argument("--test", action="store_true", help="Run local test mode with timing metrics")
    parser.add_argument("--cases", type=int, default=0, help="Limit the number of suite test cases (0 = all)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--jsonl", action="store_true", help="Force single-line JSON output")
    parser.add_argument("--reasoning-effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--max-completion-tokens", type=int, default=1480)

    args = parser.parse_args()

    if args.server:
        run_server()
        return 0

    pretty = (args.pretty or args.test) and not args.jsonl

    if args.test and args.query:
        classification, metrics, raw = classify_with_timing_cli(
            user_text=args.query,
            reasoning_effort=args.reasoning_effort,
            max_completion_tokens=args.max_completion_tokens,
        )
        emit(
            {
                "mode": "test_single",
                "input": args.query,
                "classification": classification,
                "metrics": metrics,
                "classifier_model": CLASSIFIER_MODEL,
                "raw_classifier_output": raw,
                "timestamp_unix_ms": int(time.time() * 1000),
            },
            pretty=pretty,
        )
        return 0

    if args.test:
        tests = [
            "Why is Japan so beautiful?",
            "Explain what an index is in databases.",
            "What is a knowledge graph?",
            "What are the latest AI agent frameworks released in the last 3 months?",
            "Who is the current CEO of OpenAI?",
            "What are today's headlines about Nvidia?",
            "Draft an email to my IT team asking for VPN access.",
            "Write a Python script to parse these logs and output a CSV.",
            "Open my desktop and close all Chrome tabs except Jira.",
            "Go on",
            "Why?",
            "Ok",
        ]
        if args.cases and args.cases > 0:
            tests = tests[: args.cases]

        results: List[Dict[str, Any]] = []
        ttfts: List[float] = []
        rtts: List[float] = []
        for index, query in enumerate(tests, start=1):
            classification, metrics, raw = classify_with_timing_cli(
                user_text=query,
                reasoning_effort=args.reasoning_effort,
                max_completion_tokens=args.max_completion_tokens,
            )
            ttfts.append(metrics["ttft_ms"])
            rtts.append(metrics["rtt_ms"])
            results.append(
                {
                    "case": index,
                    "input": query,
                    "classification": classification,
                    "metrics": metrics,
                    "raw_classifier_output": raw,
                }
            )

        emit(
            {
                "mode": "test_suite",
                "classifier_model": CLASSIFIER_MODEL,
                "results": results,
                "summary": {
                    "count": len(results),
                    "ttft_ms_avg": sum(ttfts) / len(ttfts),
                    "ttft_ms_min": min(ttfts),
                    "ttft_ms_max": max(ttfts),
                    "rtt_ms_avg": sum(rtts) / len(rtts),
                    "rtt_ms_min": min(rtts),
                    "rtt_ms_max": max(rtts),
                },
                "timestamp_unix_ms": int(time.time() * 1000),
            },
            pretty=pretty,
        )
        return 0

    if not args.query:
        parser.print_help()
        return 1

    classification, raw = classify_fast_cli(
        user_text=args.query,
        reasoning_effort=args.reasoning_effort,
        max_completion_tokens=args.max_completion_tokens,
    )
    emit(
        {
            "mode": "single",
            "input": args.query,
            "classification": classification,
            "classifier_model": CLASSIFIER_MODEL,
            "raw_classifier_output": raw,
            "timestamp_unix_ms": int(time.time() * 1000),
        },
        pretty=pretty,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
