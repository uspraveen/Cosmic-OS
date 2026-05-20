"""Map-agent internal LLM — parse natural-language map requests."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from .config import MapAgentConfig

logger = logging.getLogger(__name__)

_PARSE_SYSTEM = """\
You convert map requests into structured JSON for a geocoding and routing agent.

Output ONLY valid JSON:
{
  "title": "short map title",
  "subtitle": "optional one-line summary or null",
  "draw_route": true,
  "route_profile": "driving",
  "markers": [
    {
      "label": "human label",
      "query": "geocodable place text",
      "lat": null,
      "lng": null,
      "color": "#2563eb",
      "kind": "marker",
      "description": "optional note or null"
    }
  ],
  "route_waypoints": ["place A", "place B"],
  "route_alternatives": 1,
  "route_options": [
    {
      "label": "Fastest via Memphis and St. Louis",
      "route_profile": "driving",
      "route_waypoints": ["Little Rock, Arkansas", "Memphis, Tennessee", "St. Louis, Missouri", "Chicago, Illinois"],
      "color": "#dc2626",
      "description": "optional note or null"
    }
  ]
}

Rules:
- Use route_waypoints when the user wants a path between ordered stops.
- Use route_options when the user asks for multiple routes, alternative routes, corridors, or named route choices.
- For each route_options item, include the same origin and destination plus any explicit via/intermediate places.
- If the user asks for alternatives but does not name specific corridors, set route_waypoints to origin/destination and route_alternatives to 3 or 4.
- Use markers for standalone pins, highlights, or POIs that are not necessarily connected as a route.
- If both apply, include both.
- Prefer explicit geocodable queries in route_waypoints/markers.query.
- If the user already gives coordinates, fill lat/lng and keep query as the label.
- draw_route should be true when the user asks for directions, a route, path, drive, walk, or travel between places.
- route_profile must be one of: driving, walking, cycling.
- Keep lists concise and practical.
"""


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _parse_loose_json(raw: str) -> dict[str, Any] | None:
    text = _safe_text(raw)
    if not text:
        return None
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{") : text.rindex("}") + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return None


async def parse_map_request(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    query: str,
    context: str | None = None,
) -> dict[str, Any] | None:
    if not cfg.enable_internal_llm:
        return None
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None

    user_parts = [f"Map request:\n{query.strip()}"]
    if context:
        user_parts.append(f"Supporting context:\n{context.strip()[:2000]}")

    payload = {
        "model": cfg.internal_llm_model,
        "messages": [
            {"role": "system", "content": _PARSE_SYSTEM},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {cfg.internal_llm_api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = await http_client.post(
            f"{cfg.internal_llm_base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=cfg.internal_llm_timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = _safe_text(message.get("content")) if isinstance(message, dict) else ""
        return _parse_loose_json(content)
    except Exception as exc:
        logger.warning("map_agent.parse_request_llm_error: %s", exc)
        return None
