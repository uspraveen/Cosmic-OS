"""OpenStreetMap Nominatim geocoding helpers."""

from __future__ import annotations

import logging
import math
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


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lng1, lat1 = math.radians(a[0]), math.radians(a[1])
    lng2, lat2 = math.radians(b[0]), math.radians(b[1])
    d_lng = lng2 - lng1
    d_lat = lat2 - lat1
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _coerce_candidate(raw: Any, *, query: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        lat = float(raw.get("lat"))
        lng = float(raw.get("lon"))
    except (TypeError, ValueError):
        return None
    return {
        "query": query,
        "label": _safe_text(raw.get("display_name")) or query,
        "lat": lat,
        "lng": lng,
        "position": [lng, lat],
        "place_id": raw.get("place_id"),
        "category": _safe_text(raw.get("category")) or None,
        "type": _safe_text(raw.get("type")) or None,
    }


def _candidate_score(
    candidate: dict[str, Any],
    *,
    bias_coordinates: list[tuple[float, float]] | None,
) -> float:
    if not bias_coordinates:
        return 0.0
    point = (float(candidate["lng"]), float(candidate["lat"]))
    distances = [_haversine_km(point, bias) for bias in bias_coordinates]
    return sum(distances) / max(1, len(distances))


async def geocode_query(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    query: str,
    bias_coordinates: list[tuple[float, float]] | None = None,
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
            "limit": 8 if bias_coordinates else 1,
            "addressdetails": 0,
        },
        headers={"User-Agent": cfg.nominatim_user_agent},
        timeout=cfg.geocode_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise GeocodingError(query, f"No geocode result for '{text}'.")

    candidates = [
        candidate
        for candidate in (_coerce_candidate(item, query=text) for item in payload)
        if candidate
    ]
    if not candidates:
        raise GeocodingError(query, f"Invalid coordinates for '{text}'.")
    return min(
        candidates,
        key=lambda candidate: _candidate_score(
            candidate,
            bias_coordinates=bias_coordinates,
        ),
    )


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
