"""OSRM routing helpers."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import MapAgentConfig

logger = logging.getLogger(__name__)

ROUTE_COLORS = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c")


class RoutingError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


async def fetch_route(
    *,
    cfg: MapAgentConfig,
    http_client: httpx.AsyncClient,
    coordinates: list[tuple[float, float]],
    profile: str = "driving",
) -> dict[str, Any]:
    if len(coordinates) < 2:
        raise RoutingError("At least two coordinates are required for routing.")

    limited = coordinates[: cfg.max_route_waypoints]
    coord_path = ";".join(f"{lng:.6f},{lat:.6f}" for lng, lat in limited)
    base = cfg.osrm_base_url.rstrip("/")
    response = await http_client.get(
        f"{base}/route/v1/{profile}/{coord_path}",
        params={
            "overview": "full",
            "geometries": "geojson",
            "steps": "false",
        },
        timeout=cfg.route_timeout_sec,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RoutingError("Unexpected OSRM response.")

    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        code = _safe_text(payload.get("code")) or "NoRoute"
        raise RoutingError(f"OSRM could not build a route ({code}).")

    route = routes[0]
    if not isinstance(route, dict):
        raise RoutingError("Unexpected OSRM route payload.")

    geometry = route.get("geometry")
    if not isinstance(geometry, dict):
        raise RoutingError("OSRM route is missing geometry.")

    distance_m = float(route.get("distance") or 0.0)
    duration_s = float(route.get("duration") or 0.0)
    return {
        "geometry": geometry,
        "distance_m": distance_m,
        "duration_s": duration_s,
    }
