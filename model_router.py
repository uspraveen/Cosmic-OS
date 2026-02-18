#!/usr/bin/env python3
"""
model_router.py

Cosmic model router classifier using Groq + openai/gpt-oss-20b.
Outputs JSON only.

Routing rules:
- is_task                       -> "opus"
- needs_latest OR needs_citations  -> "perplexity" (unless task)
- vague/no context              -> "unknown"
- else                          -> "gemini"

CLI:
- Single query (fast, non-stream):
    python model_router.py "your question"

- Test a specific query with latency (streaming + TTFT/RTT):
    python model_router.py --test "your question"

- Test suite (streaming + latency):
    python model_router.py --test

Server Mode (HTTP/2 + pre-warming for lowest latency):
    python model_router.py --server
    
    Then call: POST http://localhost:8742/classify {"query": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ============================================================================
# Configuration
# ============================================================================

# User-requested hardcoded key (be careful if you share/commit this file)
GROQ_API_KEY = ""

CLASSIFIER_MODEL = "openai/gpt-oss-20b"
GROQ_API_BASE = "https://api.groq.com/openai/v1"

# HTTP/2 enabled client settings
HTTP2_ENABLED = True
CONNECTION_POOL_SIZE = 10
KEEPALIVE_EXPIRY = 30  # seconds

# Server settings
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8742

# ============================================================================
# Global state (for server mode)
# ============================================================================

_http_client: Optional[httpx.AsyncClient] = None
_connection_warmed: bool = False
_warmup_latency_ms: Optional[float] = None


# ============================================================================
# Helpers
# ============================================================================

def now() -> float:
    return time.perf_counter()


def strip_code_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def extract_json_object(s: str) -> Optional[Dict[str, Any]]:
    s = strip_code_fences(s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def normalize_route(route: Any) -> str:
    if not isinstance(route, str):
        return "gemini"
    route = route.strip().lower()
    # Added "unknown" to valid routes
    return route if route in ("perplexity", "gemini", "opus", "unknown") else "gemini"


def safe_float(x: Any, default: float = 0.5) -> float:
    try:
        v = float(x)
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v
    except Exception:
        return default


def build_messages(user_text: str) -> List[Dict[str, str]]:
    # IMPORTANT: Added instructions for "unknown" routing and vague context.
    system = (
        "You are a STRICT JSON-only classifier for a model router.\n"
        "Return ONLY one JSON object. No markdown. No explanations.\n\n"
        "Goal: Decide which downstream system should answer:\n"
        '  - "perplexity": use when the user likely needs up-to-date info, verification, or source-grounded claims.\n'
        '  - "gemini": use for timeless/general knowledge, explanations, theory, brainstorming.\n'
        '  - "opus": use for tasks/assistant work: drafting, coding, tool use, multi-step execution.\n'
        '  - "unknown": use ONLY if the input is vague, lacks context, is conversational filler (e.g., "go on", "ok", "hi"), or meaningless.\n\n'
        "Output schema (keys must match):\n"
        "{\n"
        '  "route": "perplexity|gemini|opus|unknown",\n'
        '  "needs_latest": true|false,\n'
        '  "needs_citations": true|false,\n'
        '  "is_task": true|false,\n'
        '  "confidence": 0.0-1.0,\n'
        '  "signals": ["max 6 short strings"]\n'
        "}\n\n"
        "Classification guidance:\n"
        "- Route 'unknown' if the input implies a vague conversation history you don't have (e.g. 'go on', 'and then?', 'why?'). Prompts like 'go on with the test' is a task and not unknown\n"
        "- is_task=true if the user asks to do/perform/make/create/fix/send/run/build/operate anything. Anything that needs an action to be performed by you is a task. Anything that would next lead to a task is also a task. Anything that addresses your capabilities is also a task. "
        "or requests code, drafts, workflows, tool actions, file edits, or multi-step assistance.\n"
        "- needs_latest=true if the answer could change over time OR depends on recent events/state:\n"
        "  * 'latest', 'current', 'today', 'this week', 'recent', 'now', '2026', releases, news, prices, laws, schedules,\n"
        "  * 'who is the CEO/president', 'what is the stock price', 'what changed', 'is X supported', etc.\n"
        "- needs_citations=true if the user is likely expecting verified/source-grounded information, EVEN if not asked:\n"
        "  * anything time-sensitive (needs_latest=true usually implies needs_citations=true)\n"
        "  * claims about current office holders, policies, regulations, pricing, security incidents, research/news\n"
        "  * comparisons of 'best right now', 'most popular', 'what's new'\n"
        "- For general explanations or timeless history/science concepts: needs_latest=false and needs_citations=false.\n\n"
        "Hard routing rules (must follow):\n"
        "- If input is vague/no-context ('go on', 'ok'), route MUST be unknown.\n"
        "- If is_task is true, route MUST be opus.\n"
        "- Else if needs_latest OR needs_citations is true, route MUST be perplexity.\n"
        "- Else route MUST be gemini.\n"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user_text}]


def enforce_rules(parsed: Dict[str, Any]) -> Dict[str, Any]:
    raw_route = normalize_route(parsed.get("route"))

    # Special handling for unknown/vague context
    if raw_route == "unknown":
        return {
            "route": "unknown",
            "needs_latest": "unknown",
            "needs_citations": "unknown",
            "is_task": "unknown",
            "confidence": safe_float(parsed.get("confidence"), 0.5),
            "signals": parsed.get("signals") if isinstance(parsed.get("signals"), list) else ["vague_input"],
        }

    # Standard handling for known routes
    out = {
        "route": raw_route,
        "needs_latest": bool(parsed.get("needs_latest")),
        "needs_citations": bool(parsed.get("needs_citations")),
        "is_task": bool(parsed.get("is_task")),
        "confidence": safe_float(parsed.get("confidence"), 0.5),
        "signals": parsed.get("signals") if isinstance(parsed.get("signals"), list) else [],
    }

    # Enforce hard rules for standard routes
    if out["is_task"]:
        out["route"] = "opus"
    elif out["needs_latest"] or out["needs_citations"]:
        out["route"] = "perplexity"
    else:
        out["route"] = "gemini"

    # Small consistency tweak: latest usually implies citations/verification
    if out["needs_latest"] and not out["is_task"]:
        out["needs_citations"] = True

    return out


def emit(obj: Dict[str, Any], pretty: bool) -> None:
    if pretty:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ============================================================================
# CLI Mode (uses Groq SDK - simple, blocking)
# ============================================================================

def classify_fast_cli(
    user_text: str,
    reasoning_effort: str,
    max_completion_tokens: int,
) -> Tuple[Dict[str, Any], str]:
    """Fast classification using Groq SDK (blocking, for CLI)."""
    from groq import Groq
    
    client = Groq(api_key=GROQ_API_KEY)
    completion = client.chat.completions.create(
        model=CLASSIFIER_MODEL,
        messages=build_messages(user_text),
        temperature=0.0,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        stream=False,
        stop=None,
    )
    raw = (completion.choices[0].message.content or "").strip()
    parsed = extract_json_object(raw)

    if parsed is None:
        parsed = {
            "route": "gemini",
            "needs_latest": False,
            "needs_citations": False,
            "is_task": False,
            "confidence": 0.35,
            "signals": ["parse_failed"],
        }

    return enforce_rules(parsed), raw


def classify_with_timing_cli(
    user_text: str,
    reasoning_effort: str,
    max_completion_tokens: int,
) -> Tuple[Dict[str, Any], Dict[str, float], str]:
    """Classification with timing metrics using Groq SDK (streaming, for --test)."""
    from groq import Groq
    
    client = Groq(api_key=GROQ_API_KEY)
    start = now()
    first_token: Optional[float] = None
    parts: List[str] = []

    completion = client.chat.completions.create(
        model=CLASSIFIER_MODEL,
        messages=build_messages(user_text),
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
    parsed = extract_json_object(raw)

    if parsed is None:
        parsed = {
            "route": "gemini",
            "needs_latest": False,
            "needs_citations": False,
            "is_task": False,
            "confidence": 0.35,
            "signals": ["parse_failed"],
        }

    classification = enforce_rules(parsed)

    ttft_ms = (first_token - start) * 1e3
    rtt_ms = (end - start) * 1e3
    metrics = {
        "ttft_ms": ttft_ms,
        "rtt_ms": rtt_ms,
        "estimated_network_latency_ms": ttft_ms,
    }
    return classification, metrics, raw


# ============================================================================
# Server Mode (HTTP/2 + pre-warming for lowest latency)
# ============================================================================

def create_http2_client() -> httpx.AsyncClient:
    """Create an httpx AsyncClient with HTTP/2 enabled and connection pooling."""
    return httpx.AsyncClient(
        http2=HTTP2_ENABLED,
        limits=httpx.Limits(
            max_connections=CONNECTION_POOL_SIZE,
            max_keepalive_connections=CONNECTION_POOL_SIZE,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        ),
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )


async def prewarm_connection() -> float:
    """Pre-warm the connection to Groq API."""
    global _http_client, _connection_warmed, _warmup_latency_ms

    if _http_client is None:
        return -1

    start = now()
    warmup_payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }

    try:
        response = await _http_client.post(
            f"{GROQ_API_BASE}/chat/completions",
            json=warmup_payload,
        )
        response.raise_for_status()
        _connection_warmed = True
        _warmup_latency_ms = (now() - start) * 1000
        print(f"[PREWARM] Connection warmed in {_warmup_latency_ms:.1f}ms (HTTP/2: {HTTP2_ENABLED})")
        return _warmup_latency_ms
    except Exception as e:
        print(f"[PREWARM] Warning: warmup failed: {e}")
        _warmup_latency_ms = -1
        return -1


async def classify_async(
    user_text: str,
    max_completion_tokens: int = 380,
) -> Tuple[Dict[str, Any], Dict[str, float], str]:
    """Classify using pre-warmed HTTP/2 connection (non-streaming for reliability)."""
    global _http_client

    if _http_client is None:
        raise RuntimeError("HTTP client not initialized")

    start = now()

    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": build_messages(user_text),
        "temperature": 0.0,
        "max_tokens": max_completion_tokens,
    }

    response = await _http_client.post(
        f"{GROQ_API_BASE}/chat/completions",
        json=payload,
    )
    response.raise_for_status()
    
    end = now()
    result = response.json()
    
    raw = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    parsed = extract_json_object(raw)

    if parsed is None:
        parsed = {
            "route": "gemini",
            "needs_latest": False,
            "needs_citations": False,
            "is_task": False,
            "confidence": 0.35,
            "signals": ["parse_failed"],
        }

    classification = enforce_rules(parsed)

    rtt_ms = (end - start) * 1000

    metrics = {
        "rtt_ms": rtt_ms,
        "connection_warmed": _connection_warmed,
        "http2_enabled": HTTP2_ENABLED,
    }

    return classification, metrics, raw


def run_server():
    """Run the FastAPI server with HTTP/2 + pre-warming."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
    import uvicorn

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _http_client
        print(f"[STARTUP] Creating HTTP/2 client (http2={HTTP2_ENABLED})...")
        _http_client = create_http2_client()
        print("[STARTUP] Pre-warming connection to Groq API...")
        await prewarm_connection()
        print(f"[STARTUP] Server ready on http://{SERVER_HOST}:{SERVER_PORT}")
        yield
        print("[SHUTDOWN] Closing HTTP client...")
        if _http_client:
            await _http_client.aclose()

    app = FastAPI(
        title="Cosmic Model Router",
        description="High-performance model router with HTTP/2 + pre-warming",
        version="2.0.0",
        lifespan=lifespan,
    )

    class ClassifyRequest(BaseModel):
        query: str
        max_completion_tokens: int = 380

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "http2_enabled": HTTP2_ENABLED,
            "connection_warmed": _connection_warmed,
            "warmup_latency_ms": _warmup_latency_ms,
        }

    @app.post("/classify")
    async def classify(body: Dict[str, Any]):
        query = body.get("query", "")
        max_tokens = body.get("max_completion_tokens", 380)
        
        classification, metrics, raw = await classify_async(
            user_text=query,
            max_completion_tokens=max_tokens,
        )
        
        # Log latency metrics
        logging.info(f"route={classification['route']} | RTT={metrics['rtt_ms']:.1f}ms | query=\"{query[:50]}{'...' if len(query) > 50 else ''}\"")

        return {
            "mode": "server",
            "input": query,
            "classification": classification,
            "metrics": metrics,
            "classifier_model": CLASSIFIER_MODEL,
            "raw_classifier_output": raw,
            "timestamp_unix_ms": int(time.time() * 1000),
        }

    @app.post("/classify/batch")
    async def classify_batch(queries: List[str]):
        tasks = [classify_async(q) for q in queries]
        results = await asyncio.gather(*tasks)
        return {
            "mode": "batch",
            "count": len(queries),
            "results": [
                {"input": q, "classification": r[0], "metrics": r[1], "raw_classifier_output": r[2]}
                for q, r in zip(queries, results)
            ],
            "timestamp_unix_ms": int(time.time() * 1000),
        }

    print("=" * 60)
    print("Cosmic Model Router Server")
    print(f"  HTTP/2 Enabled: {HTTP2_ENABLED}")
    print(f"  Connection Pool: {CONNECTION_POOL_SIZE}")
    print(f"  Keepalive: {KEEPALIVE_EXPIRY}s")
    print("=" * 60)

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Cosmic model router (JSON output).")
    parser.add_argument("query", nargs="?", help="Query to classify (optional in --test suite mode)")
    parser.add_argument("--server", action="store_true", help="Run as HTTP server with HTTP/2 + pre-warming")
    parser.add_argument("--test", action="store_true", help="Run test mode with latency metrics")
    parser.add_argument("--cases", type=int, default=0, help="Limit number of suite test cases (0 = all)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--jsonl", action="store_true", help="Force single-line JSON output")
    parser.add_argument("--reasoning-effort", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--max-completion-tokens", type=int, default=1480)

    args = parser.parse_args()

    # Server mode
    if args.server:
        run_server()
        return 0

    # Pretty by default in test mode unless --jsonl
    pretty = (args.pretty or args.test) and not args.jsonl

    # --test with a query => single latency run
    if args.test and args.query:
        classification, metrics, raw = classify_with_timing_cli(
            user_text=args.query,
            reasoning_effort=args.reasoning_effort,
            max_completion_tokens=args.max_completion_tokens,
        )
        out = {
            "mode": "test_single",
            "input": args.query,
            "classification": classification,
            "metrics": metrics,
            "classifier_model": CLASSIFIER_MODEL,
            "raw_classifier_output": raw,
            "timestamp_unix_ms": int(time.time() * 1000),
        }
        emit(out, pretty=pretty)
        return 0

    # --test without query => suite
    if args.test:
        tests = [
            # General knowledge -> gemini
            "Why is Japan so beautiful?",
            "Explain what an index is in databases.",
            "What is a knowledge graph?",
            # Latest + cited -> perplexity (even if not explicitly asking for citations)
            "What are the latest AI agent frameworks released in the last 3 months?",
            "Who is the current CEO of OpenAI?",
            "What are today's headlines about Nvidia?",
            # Task -> opus
            "Draft an email to my IT team asking for VPN access.",
            "Write a Python script to parse these logs and output a CSV.",
            "Open my desktop and close all Chrome tabs except Jira.",
            # Unknown -> unknown
            "Go on",
            "Why?",
            "Ok",
        ]
        if args.cases and args.cases > 0:
            tests = tests[: args.cases]

        results: List[Dict[str, Any]] = []
        ttfts: List[float] = []
        rtts: List[float] = []

        for i, q in enumerate(tests, start=1):
            classification, metrics, raw = classify_with_timing_cli(
                user_text=q,
                reasoning_effort=args.reasoning_effort,
                max_completion_tokens=args.max_completion_tokens,
            )
            ttfts.append(metrics["ttft_ms"])
            rtts.append(metrics["rtt_ms"])
            results.append({
                "case": i,
                "input": q,
                "classification": classification,
                "metrics": metrics,
                "raw_classifier_output": raw,
            })

        summary = {
            "count": len(results),
            "ttft_ms_avg": sum(ttfts) / len(ttfts),
            "ttft_ms_min": min(ttfts),
            "ttft_ms_max": max(ttfts),
            "rtt_ms_avg": sum(rtts) / len(rtts),
            "rtt_ms_min": min(rtts),
            "rtt_ms_max": max(rtts),
        }

        out = {
            "mode": "test_suite",
            "classifier_model": CLASSIFIER_MODEL,
            "results": results,
            "summary": summary,
            "timestamp_unix_ms": int(time.time() * 1000),
        }
        emit(out, pretty=pretty)
        return 0

    # Normal single query mode (fast, non-stream)
    if not args.query:
        parser.print_help()
        return 1

    classification, raw = classify_fast_cli(
        user_text=args.query,
        reasoning_effort=args.reasoning_effort,
        max_completion_tokens=args.max_completion_tokens,
    )

    out = {
        "mode": "single",
        "input": args.query,
        "classification": classification,
        "classifier_model": CLASSIFIER_MODEL,
        "raw_classifier_output": raw,
        "timestamp_unix_ms": int(time.time() * 1000),
    }
    emit(out, pretty=pretty)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())