"""OpenStreetMap Nominatim geocoding helpers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import MapAgentConfig

logger = logging.getLogger(__name__)


class GeocodingError(RuntimeError):
    def __init__(self, query: str, message: str) -> None:
        super().__init__(message)
        self.query = query
        self.message = message


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


async def geocode_query(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    query: str,
) -> dict[str, Any]:
    text = _safe_text(query)
    if not text:
        raise GeocodingError(query, "Geocode query is empty.")

    base = cfg.nominatim_base_url.rstrip("/")
    response = await http_client.get(
        f"{base}/search",
        params={
            "q": text,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 0,
        },
        headers={"User-Agent": cfg.nominatim_user_agent},
        timeout=cfg.geocode_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise GeocodingError(query, f"No geocode result for '{text}'.")

    top = payload[0]
    if not isinstance(top, dict):
        raise GeocodingError(query, f"Unexpected geocode payload for '{text}'.")

    try:
        lat = float(top.get("lat"))
        lng = float(top.get("lon"))
    except (TypeError, ValueError) as exc:
        raise GeocodingError(query, f"Invalid coordinates for '{text}'.") from exc

    display_name = _safe_text(top.get("display_name")) or text
    return {
        "query": text,
        "label": display_name,
        "lat": lat,
        "lng": lng,
        "position": [lng, lat],
        "place_id": top.get("place_id"),
        "category": _safe_text(top.get("category")) or None,
        "type": _safe_text(top.get("type")) or None,
    }


async def geocode_many(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    queries: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for query in queries:
        normalized = _safe_text(query)
        if not normalized:
            continue
        results.append(
            await geocode_query(cfg=cfg, http_client=http_client, query=normalized)
        )
    return results
