"""Map Agent — geocoding, routing, and interactive map artifacts for COSMIC."""

from __future__ import annotations

import hashlib
import json
import logging
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
from .internal_llm import parse_map_request
from .map_builder import build_map_spec, format_distance_meters, format_duration_seconds
from .routing import RoutingError, fetch_route

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
        lat = _coerce_float(raw.get("lat"))
        lng = _coerce_float(raw.get("lng"))
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
        route_waypoints = [
            _safe_text(item)
            for item in (payload.get("route_waypoints") if isinstance(payload.get("route_waypoints"), list) else [])
            if _safe_text(item)
        ]
        if not route_waypoints:
            stops = payload.get("stops")
            if isinstance(stops, list):
                route_waypoints = [_safe_text(item) for item in stops if _safe_text(item)]
        return {
            "title": _safe_text(payload.get("title")) or None,
            "subtitle": _safe_text(payload.get("subtitle")) or None,
            "draw_route": bool(payload.get("draw_route", bool(route_waypoints))),
            "route_profile": _safe_text(payload.get("route_profile")) or "driving",
            "markers": markers,
            "route_waypoints": route_waypoints,
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
            and not structured.get("route_waypoints")
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
                        _safe_text(item)
                        for item in (parsed.get("route_waypoints") if isinstance(parsed.get("route_waypoints"), list) else [])
                        if _safe_text(item)
                    ]
                if not structured.get("markers"):
                    structured["markers"] = [
                        item
                        for item in (
                            self._normalize_marker_input(raw)
                            for raw in (parsed.get("markers") if isinstance(parsed.get("markers"), list) else [])
                        )
                        if item
                    ]
        if query and not structured.get("title"):
            structured["title"] = query[:120]
        return structured

    async def _materialize_plan(self, plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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

        route_waypoints = [
            _safe_text(item)
            for item in (plan.get("route_waypoints") if isinstance(plan.get("route_waypoints"), list) else [])
            if _safe_text(item)
        ][: self._cfg.max_route_waypoints]

        route_coordinates: list[tuple[float, float]] = []
        for waypoint in route_waypoints:
            if waypoint in coordinate_lookup:
                route_coordinates.append(coordinate_lookup[waypoint])
                continue
            geocoded = await geocode_query(cfg=self._cfg, http_client=self._http(), query=waypoint)
            coordinate_lookup[waypoint] = (geocoded["lng"], geocoded["lat"])
            route_coordinates.append((geocoded["lng"], geocoded["lat"]))
            resolved_markers.append(
                {
                    "label": geocoded["label"],
                    "query": waypoint,
                    "position": geocoded["position"],
                    "kind": "waypoint",
                }
            )

        routes: list[dict[str, Any]] = []
        if plan.get("draw_route") and len(route_coordinates) >= 2:
            profile = _safe_text(plan.get("route_profile")) or "driving"
            if profile not in {"driving", "walking", "cycling"}:
                profile = "driving"
            route = await fetch_route(
                cfg=self._cfg,
                http_client=self._http(),
                coordinates=route_coordinates,
                profile=profile,
            )
            routes.append(
                {
                    "label": "Route",
                    "geometry": route["geometry"],
                    "distance_m": route["distance_m"],
                    "duration_s": route["duration_s"],
                }
            )

        if not resolved_markers and route_coordinates:
            for index, (lng, lat) in enumerate(route_coordinates):
                resolved_markers.append(
                    {
                        "label": route_waypoints[index] if index < len(route_waypoints) else f"Stop {index + 1}",
                        "query": route_waypoints[index] if index < len(route_waypoints) else "",
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

        return deduped_markers, routes

    async def handle_map_render(self, task: TaskEnvelope) -> AgentResult:
        query = _safe_text(task.input.get("query")) or _safe_text(task.input.get("description"))
        plan = await self._resolve_plan(task)
        if not plan.get("markers") and not plan.get("route_waypoints") and not query:
            return AgentResult(
                status="failed",
                output={},
                artifacts=[],
                error=AgentError(
                    code="INVALID_INPUT",
                    retryable=False,
                    message="map.render requires a query, markers, or route_waypoints.",
                    next_action="escalate",
                ),
            )

        markers, routes = await self._materialize_plan(plan)
        if not markers and not routes:
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
        if routes:
            distance = format_distance_meters(routes[0].get("distance_m"))
            duration = format_duration_seconds(routes[0].get("duration_s"))
            if distance and duration:
                summary_parts.append(f"Route: {distance}, about {duration}.")
            elif distance:
                summary_parts.append(f"Route distance: {distance}.")
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
