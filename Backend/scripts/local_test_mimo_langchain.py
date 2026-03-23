#!/usr/bin/env python3
"""
Smoke test: LangChain ChatOpenAI against Xiaomi MiMo (OpenAI-compatible API).

Uses the **same environment variables** as `agents/tabular_agent/config.py`
(`TabularAgentConfig`): `TABULAR_AGENT_MIMO_API_KEY`, `TABULAR_AGENT_MIMO_BASE_URL`,
optional `TABULAR_AGENT_MIMO_MODEL`, etc. Aliases `MIMO_API_KEY` / `MIMO_OPENAI_BASE_URL`
also work.

**Base URL:** must be the OpenAI API root ending in ``/v1`` only, e.g.
``https://api.mimo-v2.com/v1`` or ``https://api.xiaomimimo.com/v1``.
Do **not** append ``/chat/completions`` — the SDK adds that path.

Setup (do not commit secrets):
  copy Backend/agents/tabular_agent/agent.env.example -> agent.env and fill keys, OR
  export vars in your shell.

Run:
  - From `Backend/`: `python scripts/local_test_mimo_langchain.py`
  - From repo root (`Cosmic-OS/`): `python scripts/local_test_mimo_langchain.py` (thin wrapper calls this file)

Exit codes: 0 = OK, 2 = missing config, 1 = API/import error.

**Hardcoded block below:** optional. Paste your API key there for one-click local runs.
If the key is left empty, values fall back to `TABULAR_AGENT_MIMO_*` / `agent.env`.
**Never commit a real key** — use a placeholder or keep empty and use env only.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Local quick test (optional). Paste key here; URL must end with /v1 only
# (no /chat/completions). Or leave key "" and set env / agent.env instead.
# ---------------------------------------------------------------------------
_HARDCODED_MIMO_API_KEY = ""
# Use the base URL from your MiMo dashboard / API docs. Two common hosts:
#   https://api.xiaomimimo.com/v1   (platform.xiaomimimo.com)
#   https://api.mimo-v2.com/v1      (mimo-v2.com docs mirror)
# If you see [Errno 11001] getaddrinfo failed, DNS cannot resolve that hostname — switch URL or VPN/DNS.
_HARDCODED_MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"

# Backend/ is the import root when running from Backend/
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


def _dns_lookup_label(host: str) -> str:
    """Windows 11001 / getaddrinfo failed = this hostname does not resolve (DNS)."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        uniq = sorted({x[4][0] for x in infos})
        preview = ", ".join(uniq[:4])
        if len(uniq) > 4:
            preview += ", ..."
        return f"resolves OK -> {preview}"
    except OSError as exc:
        return f"DOES NOT RESOLVE ({type(exc).__name__}: {exc})"


def _print_mimo_dns_hints() -> None:
    """MiMo uses different marketing domains; only one may resolve from your network."""
    hosts = ("api.xiaomimimo.com", "api.mimo-v2.com")
    print("[diag] DNS from this machine (443):")
    for h in hosts:
        print(f"       {h}: {_dns_lookup_label(h)}")
    print(
        "[diag] Errno 11001 / getaddrinfo failed = DNS problem, not LangChain. "
        "Fix: set _HARDCODED_MIMO_BASE_URL to https://<host-that-resolves>/v1, "
        "try another network/VPN, change DNS (e.g. 1.1.1.1), or check corporate firewall."
    )


async def _diag_reachability(base_url: str) -> None:
    """Cheap HTTPS check; helps distinguish DNS/TLS/firewall from API/auth errors."""
    try:
        import httpx
    except ImportError:
        print("[diag] httpx not installed; skipping reachability probe.")
        return
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").strip()
    if host:
        print(f"[diag] Target host: {host} -> {_dns_lookup_label(host)}")

    try:
        async with httpx.AsyncClient(timeout=20.0, http2=False, follow_redirects=True) as client:
            r = await client.get(base_url)
            extra = ""
            if r.status_code == 404:
                extra = " — 404 on GET /v1 is normal; chat uses POST /v1/chat/completions (see successful call below)."
            print(f"[diag] GET {base_url} -> HTTP {r.status_code} (TLS reachable){extra}")
    except Exception as exc:
        print(f"[diag] GET {base_url} -> FAILED: {type(exc).__name__}: {exc}")
        if "getaddrinfo" in str(exc).lower() or "11001" in str(exc):
            _print_mimo_dns_hints()
        else:
            print(
                "[diag] Non-DNS failure: check TLS/proxy/firewall, or set HTTPS_PROXY if required."
            )


async def _run() -> int:
    try:
        import httpx
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        print("Missing dependency. Install Backend requirements (langchain-openai, langchain-core, httpx):", exc)
        return 1

    from agents.tabular_agent.config import TabularAgentConfig, normalize_mimo_openai_base_url

    cfg = TabularAgentConfig.from_env()
    api_key = (_HARDCODED_MIMO_API_KEY or "").strip() or cfg.mimo_api_key
    if (_HARDCODED_MIMO_BASE_URL or "").strip():
        base_url = normalize_mimo_openai_base_url(_HARDCODED_MIMO_BASE_URL)
    else:
        base_url = cfg.mimo_base_url

    if not api_key:
        print(
            "Set _HARDCODED_MIMO_API_KEY in this script, or TABULAR_AGENT_MIMO_API_KEY / MIMO_API_KEY / agent.env."
        )
        return 2
    if not base_url:
        print(
            "Set _HARDCODED_MIMO_BASE_URL in this script, or TABULAR_AGENT_MIMO_BASE_URL "
            "(e.g. https://api.mimo-v2.com/v1 — no /chat/completions suffix)."
        )
        return 2

    print("Testing:", base_url, "| model:", cfg.mimo_model)
    await _diag_reachability(base_url)

    messages = [
        SystemMessage(content="Reply with exactly one short sentence."),
        HumanMessage(content='Say "MiMo smoke test OK" and nothing else.'),
    ]
    try:
        # http2=False avoids some corporate proxies / middleboxes that break HTTP/2 to MiMo.
        async with httpx.AsyncClient(
            timeout=cfg.mimo_timeout_sec,
            http2=False,
            follow_redirects=True,
        ) as http_client:
            llm = ChatOpenAI(
                model=cfg.mimo_model,
                api_key=api_key,
                base_url=base_url,
                temperature=0.2,
                http_async_client=http_client,
            )
            result = await llm.ainvoke(messages)
    except Exception as exc:
        print("ChatOpenAI.ainvoke failed:", exc)
        cause = getattr(exc, "__cause__", None)
        if cause:
            print("Cause:", cause)
        print(f"Exception type: {type(exc).__name__!r}")
        import traceback

        traceback.print_exc()
        return 1

    text = getattr(result, "content", None) or str(result)
    usage = getattr(result, "usage_metadata", None) or {}
    if not usage and isinstance(getattr(result, "response_metadata", None), dict):
        rm = result.response_metadata
        usage = rm.get("token_usage") if isinstance(rm, dict) else {}

    print("Model:", cfg.mimo_model)
    print("Base URL:", base_url)
    print("Response:", str(text).strip()[:500])
    if isinstance(usage, dict) and usage:
        print("Usage metadata:", usage)
    print("OK — LangChain OpenAI client reached MiMo-compatible endpoint.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
