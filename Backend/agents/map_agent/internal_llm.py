"""Map-agent internal LLM — parse natural-language map requests."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from shared import normalized_reasoning_effort

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
  ],
  "shapes": [
    {
      "type": "rectangle",
      "label": "YC building box",
      "target": "560 20th Street, San Francisco, CA",
      "color": "#f97316",
      "fillColor": "#f97316",
      "fillOpacity": 0.16,
      "weight": 4,
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
- Use shapes when the user asks to draw a box, rectangle, circle, polygon, boundary, outline, or highlighted area on the map.
- For "box around PLACE" or "boundary around PLACE", prefer a shape with type "rectangle" and target set to a geocodable place. Do not invent geocoded strings like "northwest corner"; only use exact coordinates if the user gave them.
- Convert color names to hex, for example orange -> "#f97316".
- If both apply, include both.
- Prefer explicit geocodable queries in route_waypoints/markers.query.
- If the user already gives coordinates, fill lat/lng and keep query as the label.
- draw_route should be true when the user asks for directions, a route, path, drive, walk, or travel between places.
- route_profile must be one of: driving, walking, cycling.
- Keep lists concise and practical.
"""

_EXPAND_ROUTE_OPTIONS_SYSTEM = """\
You improve route alternatives for a routing agent.

The user or orchestrator may provide multiple abstract route labels with the
same origin and destination, such as "fastest", "shortest", or "scenic". Labels
alone do not create different routes. Convert each option into concrete,
geocodable route_waypoints.

Output ONLY valid JSON:
{
  "route_options": [
    {
      "label": "short human label",
      "route_profile": "driving",
      "route_waypoints": ["origin", "via city or landmark", "destination"],
      "color": "#2563eb",
      "description": "optional one-line explanation or null"
    }
  ]
}

Rules:
- Preserve the same origin and destination for every option.
- Add intermediate waypoints only when they are concrete, well-known, and likely geocodable.
- Prefer cities, towns, named parks, named landmarks, or named regions over highway numbers alone.
- If a label already mentions "via" or "through", include those places as waypoints.
- For "scenic", use plausible scenic corridors or named natural/cultural landmarks when obvious.
- For "fastest" or "shortest", it is valid to keep only origin and destination if no reliable via point is known.
- Do not invent obscure places.
- Keep every route_waypoints list short and practical, normally 2-5 items.
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


async def _chat_json(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    system: str,
    user_parts: list[str],
) -> dict[str, Any] | None:
    if not cfg.enable_internal_llm:
        return None
    if not cfg.internal_llm_api_key or not cfg.internal_llm_base_url:
        return None

    payload = {
        "model": cfg.internal_llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
        "response_format": {"type": "json_object"},
    }
    effort = normalized_reasoning_effort(cfg.internal_llm_model, cfg.internal_llm_reasoning_effort)
    if effort is not None:
        payload["reasoning_effort"] = effort
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


async def parse_map_request(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    query: str,
    context: str | None = None,
) -> dict[str, Any] | None:
    user_parts = [f"Map request:\n{query.strip()}"]
    if context:
        user_parts.append(f"Supporting context:\n{context.strip()[:2000]}")
    return await _chat_json(
        cfg=cfg,
        http_client=http_client,
        system=_PARSE_SYSTEM,
        user_parts=user_parts,
    )


async def expand_route_options(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    origin: str,
    destination: str,
    route_options: list[dict[str, Any]],
    context: str | None = None,
) -> list[dict[str, Any]] | None:
    options = []
    for option in route_options:
        if not isinstance(option, dict):
            continue
        options.append(
            {
                "label": _safe_text(option.get("label")),
                "description": _safe_text(option.get("description")) or None,
                "route_profile": _safe_text(option.get("route_profile")) or "driving",
                "route_waypoints": option.get("route_waypoints"),
                "color": _safe_text(option.get("color")) or None,
            }
        )
    if not options:
        return None

    user_parts = [
        "Expand these route options into concrete, geocodable waypoints.",
        f"Origin: {origin}",
        f"Destination: {destination}",
        "Route options JSON:",
        json.dumps(options, ensure_ascii=True),
    ]
    if context:
        user_parts.append(f"Supporting context:\n{context.strip()[:2000]}")

    parsed = await _chat_json(
        cfg=cfg,
        http_client=http_client,
        system=_EXPAND_ROUTE_OPTIONS_SYSTEM,
        user_parts=user_parts,
    )
    if not isinstance(parsed, dict):
        return None
    expanded = parsed.get("route_options")
    if not isinstance(expanded, list):
        return None
    return [item for item in expanded if isinstance(item, dict)]
