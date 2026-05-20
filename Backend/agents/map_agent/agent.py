"""Map Agent — geocoding, routing, and interactive map artifacts for COSMIC."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from shared.agent_runtime import AgentRuntime
from shared.contracts import AgentError, AgentResult, ArtifactManifest, TaskEnvelope, utcnow
from shared.map_artifacts import COSMIC_MAP_EXTENSION, COSMIC_MAP_MIME_TYPE
from shared.sqlite_client import connect_sync

from .config import AGENT_ROOT, MapAgentConfig
from .geocoding import GeocodingError, geocode_many, geocode_query
from .internal_llm import expand_route_options, parse_map_request
from .map_builder import build_map_spec, format_distance_meters, format_duration_seconds
from .routing import RoutingError, fetch_routes

logger = logging.getLogger(__name__)

_MAP_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS map_sessions (
    session_id TEXT,
    task_id TEXT,
    intent TEXT NOT NULL,
    title TEXT,
    marker_count INTEGER NOT NULL DEFAULT 0,
    route_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_map_sessions_session_created
ON map_sessions (session_id, created_at DESC);
"""


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_float(value: Any) -> float | None:
    try:
        if value in (None, "", []):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, "", []):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _looks_like_alternative_request(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            "alternative",
            "alternatives",
            "alternate",
            "options",
            "multiple routes",
            "different routes",
            "route choices",
        )
    )


def _waypoint_text(value: Any) -> str:
    if isinstance(value, dict):
        return (
            _safe_text(value.get("query"))
            or _safe_text(value.get("address"))
            or _safe_text(value.get("label"))
            or _safe_text(value.get("name"))
        )
    return _safe_text(value)


def _waypoint_key(waypoints: list[Any]) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", " ", _waypoint_text(item)).casefold() for item in waypoints)


def _endpoint_key(waypoints: list[Any]) -> tuple[str, str] | None:
    normalized = [_waypoint_text(item) for item in waypoints if _waypoint_text(item)]
    if len(normalized) < 2:
        return None
    return (
        re.sub(r"\s+", " ", normalized[0]).casefold(),
        re.sub(r"\s+", " ", normalized[-1]).casefold(),
    )


def _route_geometry_signature(route: dict[str, Any]) -> tuple[tuple[float, float], ...] | None:
    geometry = route.get("geometry")
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    if not isinstance(coords, list) or not coords:
        return None
    signature: list[tuple[float, float]] = []
    for coord in coords:
        if not isinstance(coord, list) or len(coord) < 2:
            continue
        lng = _coerce_float(coord[0])
        lat = _coerce_float(coord[1])
        if lng is None or lat is None:
            continue
        signature.append((round(lng, 5), round(lat, 5)))
    return tuple(signature) or None


def _looks_like_road_token(text: str) -> bool:
    normalized = _safe_text(text)
    return bool(
        re.search(
            r"\b(i[-\s]?\d+|interstate\s+\d+|us[-\s]?\d+|u\.s\.\s*\d+|hwy\s+\d+|highway\s+\d+|route\s+\d+)\b",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _extract_via_points_from_text(text: str) -> list[str]:
    source = _safe_text(text)
    if not source:
        return []
    matches = re.findall(
        r"\b(?:via|through|by way of)\s+([^;()\n]+)",
        source,
        flags=re.IGNORECASE,
    )
    via_points: list[str] = []
    for raw_match in matches:
        cleaned = re.sub(
            r"\b(?:route|option|corridor|path|drive|driving|walk|walking|cycle|cycling)\b.*$",
            "",
            raw_match,
            flags=re.IGNORECASE,
        )
        parts = re.split(r"\s*(?:,|/|&|\band\b|→|->)\s*", cleaned)
        for part in parts:
            candidate = _safe_text(part).strip(" -:;")
            if not candidate:
                continue
            if _looks_like_road_token(candidate):
                continue
            if len(candidate) < 3 or candidate.casefold() in {"north", "south", "east", "west"}:
                continue
            if candidate not in via_points:
                via_points.append(candidate)
    return via_points[:4]


def _normalize_lng(raw: dict[str, Any]) -> float | None:
    lng = _coerce_float(raw.get("lng"))
    if lng is None:
        lng = _coerce_float(raw.get("lon"))
    if lng is None:
        lng = _coerce_float(raw.get("longitude"))
    return lng


def _normalize_lat(raw: dict[str, Any]) -> float | None:
    lat = _coerce_float(raw.get("lat"))
    if lat is None:
        lat = _coerce_float(raw.get("latitude"))
    return lat


def _normalize_waypoint_input(raw: Any) -> str | dict[str, Any] | None:
    if isinstance(raw, str):
        text = _safe_text(raw)
        return text or None
    if isinstance(raw, dict):
        label = _safe_text(raw.get("label")) or _safe_text(raw.get("name"))
        query = (
            _safe_text(raw.get("query"))
            or _safe_text(raw.get("address"))
            or _safe_text(raw.get("place"))
            or label
        )
        lat = _normalize_lat(raw)
        lng = _normalize_lng(raw)
        if lat is not None and lng is not None:
            return {
                "label": label or query or f"{lat:.6f}, {lng:.6f}",
                "query": query or label or "",
                "lat": lat,
                "lng": lng,
                "position": [lng, lat],
            }
        return query or None
    return None


def _looks_like_route_option(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    for key in ("route_waypoints", "stops", "waypoints"):
        if isinstance(raw.get(key), list):
            return True
    return False


_COLOR_ALIASES = {
    "blue": "#2563eb",
    "red": "#dc2626",
    "green": "#16a34a",
    "purple": "#9333ea",
    "orange": "#f97316",
    "yellow": "#f59e0b",
    "black": "#111827",
    "white": "#ffffff",
}


def _normalize_color(value: Any, default: str = "#f97316") -> str:
    text = _safe_text(value).casefold()
    if not text:
        return default
    if text in _COLOR_ALIASES:
        return _COLOR_ALIASES[text]
    if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", text):
        return text
    return default


def _coerce_opacity(value: Any, default: float = 0.18) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(parsed, 1.0))


def _normalize_shape_point(raw: Any) -> dict[str, Any] | str | None:
    waypoint = _normalize_waypoint_input(raw)
    if waypoint:
        return waypoint
    if isinstance(raw, list) and len(raw) >= 2:
        lng = _coerce_float(raw[0])
        lat = _coerce_float(raw[1])
        if lat is not None and lng is not None:
            return {
                "label": f"{lat:.6f}, {lng:.6f}",
                "query": "",
                "lat": lat,
                "lng": lng,
                "position": [lng, lat],
            }
    return None


def _rectangle_bounds_around(lng: float, lat: float, *, radius_m: float = 120.0) -> dict[str, list[float]]:
    import math

    lat_delta = radius_m / 111_320.0
    lng_scale = max(0.2, math.cos(math.radians(lat)))
    lng_delta = radius_m / (111_320.0 * lng_scale)
    return {
        "southwest": [lng - lng_delta, lat - lat_delta],
        "northeast": [lng + lng_delta, lat + lat_delta],
    }


def _rectangle_bounds_from_points(points: list[tuple[float, float]]) -> dict[str, list[float]] | None:
    if not points:
        return None
    lngs = [point[0] for point in points]
    lats = [point[1] for point in points]
    return {
        "southwest": [min(lngs), min(lats)],
        "northeast": [max(lngs), max(lats)],
    }


def _extract_shape_request(query: str) -> dict[str, Any] | None:
    text = re.sub(r"\s+", " ", _safe_text(query))
    if not text:
        return None
    lowered = text.casefold()
    if not any(token in lowered for token in ("box", "rectangle", "boundary", "outline", "circle", "polygon")):
        return None
    color = "#f97316" if "orange" in lowered else _normalize_color(None)
    shape_type = "circle" if "circle" in lowered else "rectangle"
    patterns = (
        r"\b(?:box|rectangle|boundary|outline|circle)\s+around\s+(?P<target>.+?)(?:\s+(?:in|with)\s+\w+)?$",
        r"\baround\s+(?P<target>.+?)(?:\s+(?:in|with)\s+\w+)?$",
        r"\bshow\s+(?P<target>.+?)\s+(?:with|and)\s+(?:a\s+)?(?:box|rectangle|boundary|outline|circle)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        target = _clean_route_endpoint(match.group("target"))
        target = re.sub(r"\b(?:in|with)\s+(orange|blue|red|green|purple|yellow|black|white)\b", "", target, flags=re.IGNORECASE)
        target = target.strip(" ,.;:-")
        if target:
            return {
                "type": shape_type,
                "label": f"{target} boundary" if shape_type == "rectangle" else f"{target} radius",
                "target": target,
                "color": color,
                "fillColor": color,
                "fillOpacity": 0.16,
                "weight": 4,
            }
    return None


def _clean_route_endpoint(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", _safe_text(text))
    cleaned = re.sub(
        r"\b(driving|walking|cycling|route|routes|directions|path|with alternatives?|options?|on a map|map)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip(" ,.;:-")


def _extract_basic_route(query: str) -> tuple[str, str] | None:
    text = re.sub(r"\s+", " ", _safe_text(query))
    if not text:
        return None
    first_sentence = re.split(r"[.;\n]", text, maxsplit=1)[0]
    patterns = (
        r"\bfrom\s+(?P<origin>.+?)\s+to\s+(?P<destination>.+)$",
        r"\bbetween\s+(?P<origin>.+?)\s+and\s+(?P<destination>.+)$",
        r"^(?:show|map|draw|render|route|directions for|directions)\s+(?P<origin>.+?)\s+to\s+(?P<destination>.+)$",
        r"^(?P<origin>.+?)\s+to\s+(?P<destination>.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, first_sentence, flags=re.IGNORECASE)
        if not match:
            continue
        origin = _clean_route_endpoint(match.group("origin"))
        destination = _clean_route_endpoint(match.group("destination"))
        if origin and destination and origin.lower() != destination.lower():
            return origin, destination
    return None


def _extract_basic_place(query: str) -> str | None:
    text = re.sub(r"\s+", " ", _safe_text(query))
    if not text:
        return None
    first_sentence = re.split(r"[.;\n]", text, maxsplit=1)[0]
    cleaned = re.sub(
        r"^(show|map|locate|find|where is|where's|put|pin)\s+",
        "",
        first_sentence,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s+(on|in)\s+(a\s+)?map$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = _clean_route_endpoint(cleaned)
    return cleaned or None


class MapAgent(AgentRuntime):
    """OpenStreetMap-backed map specialist."""

    def __init__(self, redis_client, config: MapAgentConfig | None = None):
        self._cfg = config or MapAgentConfig.from_env()
        super().__init__(
            agent_card_path=str(AGENT_ROOT / "agent_card.yaml"),
            redis_client=redis_client,
        )
        self.artifacts_root = self._cfg.artifacts_root.resolve()
        self.agent_id = "cosmic/map-agent:1.0.0"
        self.db = None
        self._http_client: httpx.AsyncClient | None = None

    async def on_startup(self) -> None:
        data_dir = AGENT_ROOT / "store" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = connect_sync(str(data_dir / "map_sessions.db"))
        self.db.executescript(_MAP_SESSIONS_SQL)
        self.db.commit()
        (AGENT_ROOT / "runtime" / "logs").mkdir(parents=True, exist_ok=True)

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30, http2=False)
        return self._http_client

    def _task_artifact_dir(self, task_id: str) -> Path:
        return self.artifacts_root / task_id / "map_agent"

    def _artifact_manifest(
        self,
        *,
        task_id: str,
        path: Path,
        mime: str,
        kind: str = "output",
        audience: str = "deliverable",
    ) -> ArtifactManifest:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.artifacts_root)
            logical_path = (Path("runs") / "artifacts" / relative).as_posix()
        except ValueError:
            logical_path = resolved.as_posix()
        return ArtifactManifest(
            artifact_id=f"art_{uuid4().hex[:12]}",
            task_id=task_id,
            mime=mime,
            sha256=digest,
            path=logical_path,
            created_by_agent=self.agent_id,
            kind=kind,
            audience=audience,
        )

    async def execute(self, task: TaskEnvelope) -> AgentResult:
        handler_name = f"handle_{task.intent.replace('.', '_')}"
        handler = getattr(self, handler_name, None)
        if not handler:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message=f"Unknown intent: {task.intent}",
                    next_action="escalate",
                ),
            )
        try:
            return await handler(task)
        except GeocodingError as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message=exc.message,
                    next_action="escalate",
                ),
            )
        except RoutingError as exc:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="NETWORK_ERROR",
                    retryable=True,
                    message=exc.message,
                    next_action="retry",
                ),
            )
        except Exception as exc:
            logger.exception("map_agent.execute_failed")
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INTERNAL_ERROR",
                    retryable=False,
                    message=str(exc),
                    next_action="escalate",
                ),
            )

    def _normalize_marker_input(self, raw: Any) -> dict[str, Any] | None:
        if isinstance(raw, str):
            text = _safe_text(raw)
            return {"label": text, "query": text} if text else None
        if not isinstance(raw, dict):
            return None
        label = _safe_text(raw.get("label")) or _safe_text(raw.get("name"))
        query = _safe_text(raw.get("query")) or _safe_text(raw.get("address")) or label
        lat = _normalize_lat(raw)
        lng = _normalize_lng(raw)
        if not query and lat is None and lng is None:
            return None
        marker = {
            "label": label or query,
            "query": query or label,
            "color": _safe_text(raw.get("color")) or None,
            "kind": _safe_text(raw.get("kind")) or "marker",
            "description": _safe_text(raw.get("description")) or None,
        }
        if lat is not None and lng is not None:
            marker["lat"] = lat
            marker["lng"] = lng
            marker["position"] = [lng, lat]
        return marker

    def _normalize_route_option_input(self, raw: Any, index: int) -> dict[str, Any] | None:
        if isinstance(raw, str):
            waypoints = [
                _safe_text(item)
                for item in re.split(r"\s*(?:->|→| to )\s*", raw)
                if _safe_text(item)
            ]
            if len(waypoints) < 2:
                return None
            return {
                "label": f"Route {index + 1}",
                "route_profile": "driving",
                "route_waypoints": waypoints,
            }
        if not isinstance(raw, dict):
            return None
        raw_waypoints = (
            raw.get("route_waypoints")
            if isinstance(raw.get("route_waypoints"), list)
            else raw.get("stops")
            if isinstance(raw.get("stops"), list)
            else raw.get("waypoints")
            if isinstance(raw.get("waypoints"), list)
            else []
        )
        waypoints = [
            waypoint
            for waypoint in (_normalize_waypoint_input(item) for item in raw_waypoints)
            if waypoint
        ]
        if len(waypoints) < 2:
            return None
        label = _safe_text(raw.get("label") or raw.get("name")) or f"Route {index + 1}"
        description = _safe_text(raw.get("description")) or None
        if len(waypoints) == 2:
            via_points = _extract_via_points_from_text(" ".join([label, description or ""]))
            if via_points:
                waypoints = [waypoints[0], *via_points, waypoints[-1]]
        profile = _safe_text(raw.get("route_profile") or raw.get("profile")) or "driving"
        if profile not in {"driving", "walking", "cycling"}:
            profile = "driving"
        return {
            "label": label,
            "route_profile": profile,
            "route_waypoints": waypoints,
            "color": _safe_text(raw.get("color")) or None,
            "description": description,
            "alternatives": max(1, _coerce_int(raw.get("alternatives"), 1)),
        }

    def _normalize_shape_input(self, raw: Any, index: int) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        shape_type = _safe_text(raw.get("type") or raw.get("shape") or raw.get("kind") or "rectangle").casefold()
        if shape_type in {"box", "bbox", "boundary", "outline"}:
            shape_type = "rectangle"
        if shape_type not in {"rectangle", "polygon", "circle"}:
            shape_type = "rectangle"
        label = _safe_text(raw.get("label") or raw.get("name")) or f"Shape {index + 1}"
        color = _normalize_color(raw.get("color") or raw.get("stroke") or raw.get("strokeColor"))
        fill_color = _normalize_color(raw.get("fillColor") or raw.get("fill_color") or raw.get("fill") or color, color)
        shape: dict[str, Any] = {
            "type": shape_type,
            "label": label,
            "color": color,
            "fillColor": fill_color,
            "fillOpacity": _coerce_opacity(raw.get("fillOpacity", raw.get("fill_opacity", 0.18))),
            "weight": max(1, _coerce_int(raw.get("weight") or raw.get("stroke_width"), 3)),
            "description": _safe_text(raw.get("description")) or None,
        }

        target = (
            _safe_text(raw.get("target"))
            or _safe_text(raw.get("around"))
            or _safe_text(raw.get("query"))
            or _safe_text(raw.get("place"))
            or _safe_text(raw.get("address"))
        )
        if target:
            shape["target"] = target
        radius_m = _coerce_float(raw.get("radius_m") or raw.get("radius") or raw.get("padding_m"))
        if radius_m is not None:
            shape["radius_m"] = max(10.0, radius_m)

        bounds = raw.get("bounds")
        if isinstance(bounds, dict):
            southwest = _normalize_shape_point(bounds.get("southwest") or bounds.get("sw"))
            northeast = _normalize_shape_point(bounds.get("northeast") or bounds.get("ne"))
            if isinstance(southwest, dict) and isinstance(northeast, dict):
                shape["bounds"] = {
                    "southwest": southwest["position"],
                    "northeast": northeast["position"],
                }
        west = _coerce_float(raw.get("west") or raw.get("min_lng") or raw.get("min_lon"))
        east = _coerce_float(raw.get("east") or raw.get("max_lng") or raw.get("max_lon"))
        south = _coerce_float(raw.get("south") or raw.get("min_lat"))
        north = _coerce_float(raw.get("north") or raw.get("max_lat"))
        if all(value is not None for value in (west, east, south, north)):
            shape["bounds"] = {"southwest": [west, south], "northeast": [east, north]}

        northwest = _normalize_shape_point(raw.get("northwest") or raw.get("nw"))
        southeast = _normalize_shape_point(raw.get("southeast") or raw.get("se"))
        if isinstance(northwest, dict) and isinstance(southeast, dict):
            west = min(float(northwest["lng"]), float(southeast["lng"]))
            east = max(float(northwest["lng"]), float(southeast["lng"]))
            south = min(float(northwest["lat"]), float(southeast["lat"]))
            north = max(float(northwest["lat"]), float(southeast["lat"]))
            shape["bounds"] = {"southwest": [west, south], "northeast": [east, north]}

        raw_coordinates = raw.get("coordinates") or raw.get("points") or raw.get("corners")
        if isinstance(raw_coordinates, list):
            coordinates: list[Any] = []
            for point in raw_coordinates:
                normalized_point = _normalize_shape_point(point)
                if normalized_point:
                    coordinates.append(normalized_point)
            if len(coordinates) >= 2:
                shape["coordinates"] = coordinates

        center = _normalize_shape_point(raw.get("center"))
        if isinstance(center, dict):
            shape["center"] = center
        if shape_type == "circle" and "radius_m" not in shape:
            shape["radius_m"] = 160.0

        if any(key in shape for key in ("target", "bounds", "coordinates", "center")):
            return shape
        return None

    def _fallback_plan_from_query(self, query: str) -> dict[str, Any]:
        route = _extract_basic_route(query)
        if route:
            origin, destination = route
            alternatives = 3 if _looks_like_alternative_request(query) else 1
            return {
                "title": f"{origin} to {destination}",
                "draw_route": True,
                "route_profile": "driving",
                "route_waypoints": [origin, destination],
                "route_alternatives": alternatives,
            }
        shape = _extract_shape_request(query)
        if shape:
            target = _safe_text(shape.get("target"))
            return {
                "title": f"{target} map" if target else "Map highlight",
                "markers": [{"label": target, "query": target, "kind": "marker"}] if target else [],
                "shapes": [shape],
            }
        place = _extract_basic_place(query)
        if place:
            return {
                "title": place,
                "markers": [{"label": place, "query": place, "kind": "marker"}],
            }
        return {}

    async def _expand_ambiguous_route_options(
        self,
        plan: dict[str, Any],
        *,
        context: str | None = None,
    ) -> None:
        route_options = [
            item
            for item in (
                self._normalize_route_option_input(raw, index)
                for index, raw in enumerate(
                    plan.get("route_options") if isinstance(plan.get("route_options"), list) else []
                )
            )
            if item
        ]
        if len(route_options) < 2:
            return

        grouped: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        for index, option in enumerate(route_options):
            waypoints = option.get("route_waypoints")
            if not isinstance(waypoints, list) or len(waypoints) != 2:
                continue
            key = _endpoint_key([_safe_text(item) for item in waypoints])
            if key:
                grouped.setdefault(key, []).append((index, option))

        if not any(len(items) > 1 for items in grouped.values()):
            plan["route_options"] = route_options
            return

        refined = list(route_options)
        for group in grouped.values():
            if len(group) < 2:
                continue
            _, first_option = group[0]
            waypoints = first_option["route_waypoints"]
            origin = _waypoint_text(waypoints[0])
            destination = _waypoint_text(waypoints[-1])
            if not origin or not destination:
                continue
            expanded = await expand_route_options(
                cfg=self._cfg,
                http_client=self._http(),
                origin=origin,
                destination=destination,
                route_options=[option for _, option in group],
                context=context,
            )
            expanded_options = [
                item
                for item in (
                    self._normalize_route_option_input(raw, index)
                    for index, raw in enumerate(expanded or [])
                )
                if item
            ]
            if not expanded_options:
                continue
            expanded_keys = {
                _waypoint_key(option["route_waypoints"])
                for option in expanded_options
                if isinstance(option.get("route_waypoints"), list)
            }
            has_added_constraints = any(
                isinstance(option.get("route_waypoints"), list)
                and len(option["route_waypoints"]) > 2
                for option in expanded_options
            )
            if not has_added_constraints and len(expanded_keys) <= 1:
                continue
            for offset, (original_index, _) in enumerate(group):
                if offset < len(expanded_options):
                    refined[original_index] = expanded_options[offset]

        plan["route_options"] = refined

    def _structured_plan_from_input(self, task: TaskEnvelope) -> dict[str, Any]:
        payload = task.input if isinstance(task.input, dict) else {}
        markers = [
            item
            for item in (
                self._normalize_marker_input(raw)
                for raw in (payload.get("markers") if isinstance(payload.get("markers"), list) else [])
            )
            if item
        ]
        shape_inputs: list[Any] = []
        for key in ("shapes", "overlays", "drawings", "areas", "boundaries"):
            value = payload.get(key)
            if isinstance(value, list):
                shape_inputs.extend(value)
        shapes = [
            item
            for item in (
                self._normalize_shape_input(raw, index)
                for index, raw in enumerate(shape_inputs)
            )
            if item
        ]
        raw_route_waypoints = (
            payload.get("route_waypoints")
            if isinstance(payload.get("route_waypoints"), list)
            else payload.get("stops")
            if isinstance(payload.get("stops"), list)
            else payload.get("waypoints")
            if isinstance(payload.get("waypoints"), list)
            else []
        )
        route_waypoint_objects_are_options = any(_looks_like_route_option(item) for item in raw_route_waypoints)
        route_waypoints = [] if route_waypoint_objects_are_options else [
            waypoint
            for waypoint in (_normalize_waypoint_input(item) for item in raw_route_waypoints)
            if waypoint
        ]
        route_option_inputs = list(
            payload.get("route_options") if isinstance(payload.get("route_options"), list) else []
        )
        if route_waypoint_objects_are_options:
            route_option_inputs.extend(raw_route_waypoints)
        route_options = [
            item
            for item in (
                self._normalize_route_option_input(raw, index)
                for index, raw in enumerate(route_option_inputs)
            )
            if item
        ]
        alternatives = _coerce_int(
            payload.get("route_alternatives")
            or payload.get("alternatives")
            or payload.get("alternative_count"),
            1,
        )
        return {
            "title": _safe_text(payload.get("title")) or None,
            "subtitle": _safe_text(payload.get("subtitle")) or None,
            "draw_route": bool(payload.get("draw_route", bool(route_waypoints or route_options))),
            "route_profile": _safe_text(payload.get("route_profile")) or "driving",
            "markers": markers,
            "shapes": shapes,
            "route_waypoints": route_waypoints,
            "route_options": route_options,
            "route_alternatives": max(1, min(alternatives, 6)),
        }

    async def _resolve_plan(self, task: TaskEnvelope) -> dict[str, Any]:
        structured = self._structured_plan_from_input(task)
        query = _safe_text(task.input.get("query")) or _safe_text(task.input.get("description"))
        context = _safe_text(task.input.get("context"))
        has_coords = any(
            isinstance(marker, dict) and marker.get("position")
            for marker in structured.get("markers", [])
        )
        needs_llm = bool(query) and (
            not structured.get("markers")
            and not structured.get("shapes")
            and not structured.get("route_waypoints")
            and not structured.get("route_options")
            or (query and not has_coords)
        )
        if needs_llm:
            parsed = await parse_map_request(
                cfg=self._cfg,
                http_client=self._http(),
                query=query,
                context=context,
            )
            if isinstance(parsed, dict):
                if not structured.get("title"):
                    structured["title"] = _safe_text(parsed.get("title")) or None
                if not structured.get("subtitle"):
                    structured["subtitle"] = _safe_text(parsed.get("subtitle")) or None
                if parsed.get("draw_route") is not None:
                    structured["draw_route"] = bool(parsed.get("draw_route"))
                profile = _safe_text(parsed.get("route_profile"))
                if profile:
                    structured["route_profile"] = profile
                if not structured.get("route_waypoints"):
                    structured["route_waypoints"] = [
                        waypoint
                        for item in (parsed.get("route_waypoints") if isinstance(parsed.get("route_waypoints"), list) else [])
                        if (waypoint := _normalize_waypoint_input(item))
                    ]
                if not structured.get("route_options"):
                    structured["route_options"] = [
                        item
                        for item in (
                            self._normalize_route_option_input(raw, index)
                            for index, raw in enumerate(
                                parsed.get("route_options") if isinstance(parsed.get("route_options"), list) else []
                            )
                        )
                        if item
                    ]
                parsed_alternatives = _coerce_int(
                    parsed.get("route_alternatives")
                    or parsed.get("alternatives")
                    or parsed.get("alternative_count"),
                    0,
                )
                if parsed_alternatives > 1:
                    structured["route_alternatives"] = max(1, min(parsed_alternatives, 6))
                if not structured.get("markers"):
                    structured["markers"] = [
                        item
                        for item in (
                            self._normalize_marker_input(raw)
                            for raw in (parsed.get("markers") if isinstance(parsed.get("markers"), list) else [])
                        )
                        if item
                    ]
                if not structured.get("shapes"):
                    structured["shapes"] = [
                        item
                        for item in (
                            self._normalize_shape_input(raw, index)
                            for index, raw in enumerate(
                                parsed.get("shapes") if isinstance(parsed.get("shapes"), list) else []
                            )
                        )
                        if item
                    ]
        if (
            query
            and not structured.get("markers")
            and not structured.get("shapes")
            and not structured.get("route_waypoints")
            and not structured.get("route_options")
        ):
            fallback = self._fallback_plan_from_query(query)
            for key, value in fallback.items():
                if key == "route_alternatives" and _coerce_int(value, 1) > _coerce_int(
                    structured.get(key),
                    1,
                ):
                    structured[key] = value
                elif value and not structured.get(key):
                    structured[key] = value
        if query and not structured.get("shapes"):
            shape = _extract_shape_request(query)
            if shape:
                structured["shapes"] = [shape]
                target = _safe_text(shape.get("target"))
                if target and not structured.get("markers"):
                    structured["markers"] = [{"label": target, "query": target, "kind": "marker"}]
        if structured.get("route_options"):
            await self._expand_ambiguous_route_options(structured, context=context)
        if query and not structured.get("title"):
            structured["title"] = query[:120]
        return structured

    async def _materialize_plan(
        self,
        plan: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        resolved_markers: list[dict[str, Any]] = []
        coordinate_lookup: dict[str, tuple[float, float]] = {}

        async def ensure_position(marker: dict[str, Any]) -> dict[str, Any] | None:
            if isinstance(marker.get("position"), list) and len(marker["position"]) >= 2:
                return marker
            query = _safe_text(marker.get("query")) or _safe_text(marker.get("label"))
            lat = _coerce_float(marker.get("lat"))
            lng = _coerce_float(marker.get("lng"))
            if lat is not None and lng is not None:
                marker["position"] = [lng, lat]
                return marker
            if not query:
                return None
            if query in coordinate_lookup:
                lng, lat = coordinate_lookup[query]
                marker["position"] = [lng, lat]
                return marker
            geocoded = await geocode_query(cfg=self._cfg, http_client=self._http(), query=query)
            coordinate_lookup[query] = (geocoded["lng"], geocoded["lat"])
            marker["position"] = geocoded["position"]
            marker["label"] = marker.get("label") or geocoded["label"]
            return marker

        for marker in plan.get("markers", [])[: self._cfg.max_markers]:
            if not isinstance(marker, dict):
                continue
            resolved = await ensure_position(dict(marker))
            if resolved:
                resolved_markers.append(resolved)

        async def resolve_waypoint(
            waypoint: Any,
            *,
            bias_coordinates: list[tuple[float, float]] | None = None,
        ) -> tuple[float, float]:
            query = _waypoint_text(waypoint)
            if isinstance(waypoint, dict):
                lat = _normalize_lat(waypoint)
                lng = _normalize_lng(waypoint)
                if lat is not None and lng is not None:
                    key = query or f"{lat:.6f}, {lng:.6f}"
                    coordinate_lookup[key] = (lng, lat)
                    resolved_markers.append(
                        {
                            "label": _safe_text(waypoint.get("label")) or key,
                            "query": query,
                            "position": [lng, lat],
                            "kind": "waypoint",
                        }
                    )
                    return (lng, lat)
            if query in coordinate_lookup:
                return coordinate_lookup[query]
            geocoded = await geocode_query(
                cfg=self._cfg,
                http_client=self._http(),
                query=query,
                bias_coordinates=bias_coordinates,
            )
            coordinate_lookup[query] = (geocoded["lng"], geocoded["lat"])
            resolved_markers.append(
                {
                    "label": geocoded["label"],
                    "query": query,
                    "position": geocoded["position"],
                    "kind": "waypoint",
                }
            )
            return coordinate_lookup[query]

        async def resolve_waypoints(route_waypoints: list[Any]) -> list[tuple[float, float]]:
            limited_waypoints = route_waypoints[: self._cfg.max_route_waypoints]
            if len(limited_waypoints) >= 3:
                origin = await resolve_waypoint(limited_waypoints[0])
                destination = await resolve_waypoint(limited_waypoints[-1])
                bias_coordinates = [origin, destination]
                middle = [
                    await resolve_waypoint(waypoint, bias_coordinates=bias_coordinates)
                    for waypoint in limited_waypoints[1:-1]
                ]
                return [origin, *middle, destination]

            route_coordinates: list[tuple[float, float]] = []
            for waypoint in limited_waypoints:
                key = _waypoint_text(waypoint)
                if key in coordinate_lookup:
                    route_coordinates.append(coordinate_lookup[key])
                    continue
                route_coordinates.append(await resolve_waypoint(waypoint))
            return route_coordinates

        async def resolve_shape(raw_shape: Any, index: int) -> dict[str, Any] | None:
            shape = self._normalize_shape_input(raw_shape, index)
            if not shape:
                return None
            shape_type = _safe_text(shape.get("type")) or "rectangle"
            resolved: dict[str, Any] = {
                "type": shape_type,
                "label": _safe_text(shape.get("label")) or f"Shape {index + 1}",
                "color": _safe_text(shape.get("color")) or "#f97316",
                "fillColor": _safe_text(shape.get("fillColor")) or _safe_text(shape.get("color")) or "#f97316",
                "fillOpacity": _coerce_opacity(shape.get("fillOpacity"), 0.18),
                "weight": max(1, _coerce_int(shape.get("weight"), 3)),
                "description": _safe_text(shape.get("description")) or None,
            }
            bounds = shape.get("bounds")
            if isinstance(bounds, dict):
                southwest = bounds.get("southwest")
                northeast = bounds.get("northeast")
                if (
                    isinstance(southwest, list)
                    and len(southwest) >= 2
                    and isinstance(northeast, list)
                    and len(northeast) >= 2
                ):
                    resolved["type"] = "rectangle"
                    resolved["bounds"] = {
                        "southwest": [float(southwest[0]), float(southwest[1])],
                        "northeast": [float(northeast[0]), float(northeast[1])],
                    }
                    return resolved

            coordinates: list[tuple[float, float]] = []
            raw_coordinates = shape.get("coordinates")
            if isinstance(raw_coordinates, list):
                for point in raw_coordinates[: self._cfg.max_route_waypoints]:
                    if isinstance(point, dict) and isinstance(point.get("position"), list):
                        position = point["position"]
                        if len(position) >= 2:
                            coordinates.append((float(position[0]), float(position[1])))
                            continue
                    coordinates.append(await resolve_waypoint(point))
                if shape_type == "rectangle" and len(coordinates) >= 2:
                    bounds_from_points = _rectangle_bounds_from_points(coordinates)
                    if bounds_from_points:
                        resolved["bounds"] = bounds_from_points
                        return resolved
                if len(coordinates) >= 3:
                    resolved["type"] = "polygon"
                    resolved["coordinates"] = [[lng, lat] for lng, lat in coordinates]
                    return resolved

            center = shape.get("center")
            if center:
                lng, lat = await resolve_waypoint(center)
                if shape_type == "circle":
                    resolved["center"] = [lng, lat]
                    resolved["radius_m"] = _coerce_float(shape.get("radius_m")) or 160.0
                    return resolved
                resolved["bounds"] = _rectangle_bounds_around(
                    lng,
                    lat,
                    radius_m=_coerce_float(shape.get("radius_m")) or 120.0,
                )
                return resolved

            target = _safe_text(shape.get("target"))
            if target:
                lng, lat = await resolve_waypoint(target)
                if shape_type == "circle":
                    resolved["center"] = [lng, lat]
                    resolved["radius_m"] = _coerce_float(shape.get("radius_m")) or 160.0
                    return resolved
                resolved["type"] = "rectangle"
                resolved["bounds"] = _rectangle_bounds_around(
                    lng,
                    lat,
                    radius_m=_coerce_float(shape.get("radius_m")) or 120.0,
                )
                return resolved
            return None

        resolved_shapes: list[dict[str, Any]] = []
        for index, raw in enumerate(plan.get("shapes") if isinstance(plan.get("shapes"), list) else []):
            shape = await resolve_shape(raw, index)
            if shape:
                resolved_shapes.append(shape)

        routes: list[dict[str, Any]] = []
        seen_route_signatures: set[tuple[tuple[float, float], ...]] = set()

        def append_route(
            route: dict[str, Any],
            *,
            label: str,
            color: str | None = None,
            description: str | None = None,
            width: int = 5,
        ) -> bool:
            signature = _route_geometry_signature(route)
            if signature and signature in seen_route_signatures:
                return False
            if signature:
                seen_route_signatures.add(signature)
            routes.append(
                {
                    "label": label,
                    "color": color,
                    "description": description,
                    "geometry": route["geometry"],
                    "distance_m": route["distance_m"],
                    "duration_s": route["duration_s"],
                    "width": width,
                }
            )
            return True

        route_options = [
            item
            for item in (
                self._normalize_route_option_input(raw, index)
                for index, raw in enumerate(
                    plan.get("route_options") if isinstance(plan.get("route_options"), list) else []
                )
            )
            if item
        ]
        if route_options:
            grouped_options: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
            for option_index, option in enumerate(route_options):
                grouped_options.setdefault(_waypoint_key(option["route_waypoints"]), []).append(
                    (option_index, option)
                )

            for option_group in grouped_options.values():
                first_option_index, first_option = option_group[0]
                route_waypoints = first_option["route_waypoints"]
                route_coordinates = await resolve_waypoints(route_waypoints)
                if len(route_coordinates) < 2:
                    continue
                alternatives = max(
                    1,
                    max(
                        _coerce_int(option.get("alternatives"), 1)
                        for _, option in option_group
                    ),
                    len(option_group),
                )
                fetched_routes = await fetch_routes(
                    cfg=self._cfg,
                    http_client=self._http(),
                    coordinates=route_coordinates,
                    profile=first_option["route_profile"],
                    alternatives=alternatives,
                )
                for variant_index, route in enumerate(fetched_routes):
                    option_index, option = option_group[min(variant_index, len(option_group) - 1)]
                    label = option["label"]
                    if len(option_group) == 1 and len(fetched_routes) > 1:
                        label = f"{label} alt {variant_index + 1}"
                    elif variant_index >= len(option_group):
                        label = f"{first_option['label']} alt {variant_index + 1}"
                    append_route(
                        route,
                        label=label,
                        color=option.get("color"),
                        description=option.get("description"),
                        width=6 if first_option_index == 0 and variant_index == 0 else 5,
                    )

        route_waypoints = [
            waypoint
            for item in (plan.get("route_waypoints") if isinstance(plan.get("route_waypoints"), list) else [])
            if (waypoint := _normalize_waypoint_input(item))
        ][: self._cfg.max_route_waypoints]
        route_coordinates: list[tuple[float, float]] = []
        if not route_options:
            route_coordinates = await resolve_waypoints(route_waypoints)

        if not route_options and plan.get("draw_route") and len(route_coordinates) >= 2:
            profile = _safe_text(plan.get("route_profile")) or "driving"
            if profile not in {"driving", "walking", "cycling"}:
                profile = "driving"
            fetched_routes = await fetch_routes(
                cfg=self._cfg,
                http_client=self._http(),
                coordinates=route_coordinates,
                profile=profile,
                alternatives=max(1, _coerce_int(plan.get("route_alternatives"), 1)),
            )
            for index, route in enumerate(fetched_routes):
                append_route(
                    route,
                    label="Route" if index == 0 else f"Alternative {index + 1}",
                    width=6 if index == 0 else 5,
                )

        if not resolved_markers and route_coordinates:
            for index, (lng, lat) in enumerate(route_coordinates):
                resolved_markers.append(
                    {
                        "label": _waypoint_text(route_waypoints[index]) if index < len(route_waypoints) else f"Stop {index + 1}",
                        "query": _waypoint_text(route_waypoints[index]) if index < len(route_waypoints) else "",
                        "position": [lng, lat],
                        "kind": "waypoint",
                    }
                )

        deduped_markers: list[dict[str, Any]] = []
        seen_positions: set[tuple[float, float]] = set()
        for marker in resolved_markers:
            position = marker.get("position")
            if not isinstance(position, list) or len(position) < 2:
                continue
            key = (round(float(position[0]), 5), round(float(position[1]), 5))
            if key in seen_positions:
                continue
            seen_positions.add(key)
            deduped_markers.append(marker)

        return deduped_markers, routes, resolved_shapes

    async def handle_map_render(self, task: TaskEnvelope) -> AgentResult:
        query = _safe_text(task.input.get("query")) or _safe_text(task.input.get("description"))
        plan = await self._resolve_plan(task)
        if (
            not plan.get("markers")
            and not plan.get("shapes")
            and not plan.get("route_waypoints")
            and not plan.get("route_options")
            and not query
        ):
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="map.render requires a query, markers, shapes, route_waypoints, or route_options.",
                    next_action="escalate",
                ),
            )

        markers, routes, shapes = await self._materialize_plan(plan)
        if not markers and not routes and not shapes:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="Could not resolve any map locations from the request.",
                    next_action="escalate",
                ),
            )

        title = _safe_text(plan.get("title")) or query or "Map"
        map_spec = build_map_spec(
            title=title,
            subtitle=_safe_text(plan.get("subtitle")) or None,
            markers=markers,
            routes=routes,
            shapes=shapes,
        )

        task_dir = self._task_artifact_dir(task.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        map_path = task_dir / f"map{COSMIC_MAP_EXTENSION}"
        map_path.write_text(json.dumps(map_spec, indent=2), encoding="utf-8")
        artifact = self._artifact_manifest(
            task_id=task.task_id,
            path=map_path,
            mime=COSMIC_MAP_MIME_TYPE,
        )

        summary_parts = [f"Interactive map ready: {title}."]
        if shapes:
            summary_parts.append(f"Shapes: {len(shapes)} overlay{'s' if len(shapes) != 1 else ''}.")
        if routes:
            if len(routes) > 1:
                summary_parts.append(f"Routes: {len(routes)} options.")
            for route in routes[:4]:
                distance = format_distance_meters(route.get("distance_m"))
                duration = format_duration_seconds(route.get("duration_s"))
                label = _safe_text(route.get("label")) or "Route"
                if distance and duration:
                    summary_parts.append(f"{label}: {distance}, about {duration}.")
                elif distance:
                    summary_parts.append(f"{label}: {distance}.")
        summary_parts.append(f"[[artifact:{map_path.name}]]")
        summary = " ".join(summary_parts)

        try:
            self.db.execute(
                """INSERT OR REPLACE INTO map_sessions
                   (session_id, task_id, intent, title, marker_count, route_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    task.session_id,
                    task.task_id,
                    task.intent,
                    title,
                    len(markers),
                    len(routes),
                    utcnow().isoformat(),
                ],
            )
            self.db.commit()
        except Exception as exc:
            logger.warning("map_agent.session_save_failed: %s", exc)

        return AgentResult(
            status="completed",
            output={
                "title": title,
                "summary": summary,
                "marker_count": len(markers),
                "route_count": len(routes),
                "shape_count": len(shapes),
                "map_artifact_id": artifact.artifact_id,
            },
            artifacts=[artifact],
        )

    async def handle_map_recall_session(self, task: TaskEnvelope) -> AgentResult:
        session_id = task.input.get("session_id") or task.session_id
        limit = int(task.input.get("limit") or 10)
        rows = self.db.execute(
            """SELECT * FROM map_sessions
               WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            [session_id, max(1, min(limit, 50))],
        ).fetchall()
        return AgentResult(
            status="completed",
            output={"session_id": session_id, "entries": [dict(row) for row in rows]},
            artifacts=[],
        )
