"""Canonical chat-route tokens shared by gateway, orchestrator, and model router.

A route names the answering surface, never the model: the orchestrator route
is answered by whichever brain is currently configured (Fireworks GLM, Fireworks
Kimi, Anthropic Claude, or anything added later). The historical token "opus"
named this route after the model that originally powered it; it is accepted as
a legacy alias anywhere a route string enters, but new writes emit the
canonical token.
"""

from __future__ import annotations

from typing import Any

ORCHESTRATOR_ROUTE = "orchestrator"
DIRECT_HAIKU_ROUTE = "haiku"
RESEARCH_ROUTE = "perplexity"

CANONICAL_ROUTES = {ORCHESTRATOR_ROUTE, DIRECT_HAIKU_ROUTE, RESEARCH_ROUTE}

# Tokens that may still arrive from stored rows, in-flight events, or clients,
# mapped to their canonical equivalents.
LEGACY_ROUTE_ALIASES = {
    "opus": ORCHESTRATOR_ROUTE,
    "gemini": DIRECT_HAIKU_ROUTE,
}


def canonical_route(value: Any) -> str:
    """Lowercase a route token and resolve legacy aliases. "" stays ""."""
    normalized = str(value or "").strip().lower()
    return LEGACY_ROUTE_ALIASES.get(normalized, normalized)


def normalize_route(value: Any, *, default: str = ORCHESTRATOR_ROUTE) -> str:
    """Canonical route for any input; unknown values fall back to `default`."""
    route = canonical_route(value)
    return route if route in CANONICAL_ROUTES else default


def is_orchestrator_route(value: Any) -> bool:
    """True when the token names the orchestrator route (canonical or legacy)."""
    return canonical_route(value) == ORCHESTRATOR_ROUTE
