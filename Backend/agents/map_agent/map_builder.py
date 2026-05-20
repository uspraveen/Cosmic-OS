"""Assemble COSMIC interactive map JSON payloads."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .routing import ROUTE_COLORS

MARKER_COLORS = ("#2563eb", "#dc2626", "#16a34a", "#9333ea", "#f59e0b", "#0d9488")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _marker_id(prefix: str = "marker") -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _route_id(prefix: str = "route") -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _shape_id(prefix: str = "shape") -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _bounds_from_positions(positions: list[list[float]]) -> dict[str, list[float]] | None:
    if not positions:
        return None
    lngs = [float(item[0]) for item in positions]
    lats = [float(item[1]) for item in positions]
    return {
        "southwest": [min(lngs), min(lats)],
        "northeast": [max(lngs), max(lats)],
    }


def _fit_center_zoom(
    positions: list[list[float]],
    *,
    default_center: list[float] | None = None,
    default_zoom: int = 11,
) -> tuple[list[float], int]:
    if not positions:
        center = default_center or [0.0, 0.0]
        return center, default_zoom

    lngs = [float(item[0]) for item in positions]
    lats = [float(item[1]) for item in positions]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)
    center = [(min_lng + max_lng) / 2.0, (min_lat + max_lat) / 2.0]

    lat_span = max(0.0001, max_lat - min_lat)
    lng_span = max(0.0001, max_lng - min_lng)
    span = max(lat_span, lng_span)
    if span > 20:
        zoom = 4
    elif span > 10:
        zoom = 5
    elif span > 5:
        zoom = 6
    elif span > 2:
        zoom = 7
    elif span > 1:
        zoom = 8
    elif span > 0.5:
        zoom = 9
    elif span > 0.2:
        zoom = 10
    elif span > 0.08:
        zoom = 11
    elif span > 0.03:
        zoom = 12
    elif span > 0.01:
        zoom = 13
    else:
        zoom = 14
    return center, zoom


def build_map_spec(
    *,
    title: str,
    markers: list[dict[str, Any]],
    routes: list[dict[str, Any]] | None = None,
    shapes: list[dict[str, Any]] | None = None,
    subtitle: str | None = None,
) -> dict[str, Any]:
    normalized_markers: list[dict[str, Any]] = []
    positions: list[list[float]] = []

    for index, marker in enumerate(markers):
        if not isinstance(marker, dict):
            continue
        label = _safe_text(marker.get("label")) or _safe_text(marker.get("query")) or f"Point {index + 1}"
        position = marker.get("position")
        if not isinstance(position, list) or len(position) < 2:
            lat = marker.get("lat")
            lng = marker.get("lng")
            if lat is None or lng is None:
                continue
            position = [float(lng), float(lat)]
        else:
            position = [float(position[0]), float(position[1])]

        positions.append(position)
        normalized_markers.append(
            {
                "id": _safe_text(marker.get("id")) or _marker_id(),
                "label": label,
                "position": position,
                "color": _safe_text(marker.get("color"))
                or MARKER_COLORS[index % len(MARKER_COLORS)],
                "kind": _safe_text(marker.get("kind")) or "marker",
                "description": _safe_text(marker.get("description")) or None,
            }
        )

    normalized_routes: list[dict[str, Any]] = []
    for index, route in enumerate(routes or []):
        if not isinstance(route, dict):
            continue
        geometry = route.get("geometry")
        if not isinstance(geometry, dict):
            continue
        coords = geometry.get("coordinates")
        if isinstance(coords, list):
            for coord in coords:
                if isinstance(coord, list) and len(coord) >= 2:
                    positions.append([float(coord[0]), float(coord[1])])

        normalized_routes.append(
            {
                "id": _safe_text(route.get("id")) or _route_id(),
                "label": _safe_text(route.get("label")) or f"Route {index + 1}",
                "color": _safe_text(route.get("color"))
                or ROUTE_COLORS[index % len(ROUTE_COLORS)],
                "width": int(route.get("width") or 5),
                "geometry": geometry,
                "distance_m": route.get("distance_m"),
                "duration_s": route.get("duration_s"),
            }
        )

    normalized_shapes: list[dict[str, Any]] = []
    for index, shape in enumerate(shapes or []):
        if not isinstance(shape, dict):
            continue
        shape_type = _safe_text(shape.get("type") or shape.get("kind") or "polygon").casefold()
        if shape_type not in {"rectangle", "polygon", "circle"}:
            shape_type = "polygon"
        normalized: dict[str, Any] = {
            "id": _safe_text(shape.get("id")) or _shape_id(),
            "type": shape_type,
            "label": _safe_text(shape.get("label")) or f"Shape {index + 1}",
            "color": _safe_text(shape.get("color")) or "#f97316",
            "fillColor": _safe_text(shape.get("fillColor") or shape.get("fill_color")) or _safe_text(shape.get("color")) or "#f97316",
            "fillOpacity": shape.get("fillOpacity", shape.get("fill_opacity", 0.18)),
            "weight": int(shape.get("weight") or 3),
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
                sw = [float(southwest[0]), float(southwest[1])]
                ne = [float(northeast[0]), float(northeast[1])]
                normalized["bounds"] = {"southwest": sw, "northeast": ne}
                positions.extend([sw, ne])

        coordinates = shape.get("coordinates")
        if isinstance(coordinates, list):
            normalized_coordinates: list[list[float]] = []
            for coord in coordinates:
                if isinstance(coord, list) and len(coord) >= 2:
                    point = [float(coord[0]), float(coord[1])]
                    normalized_coordinates.append(point)
                    positions.append(point)
            if normalized_coordinates:
                normalized["coordinates"] = normalized_coordinates

        center = shape.get("center")
        if isinstance(center, list) and len(center) >= 2:
            normalized["center"] = [float(center[0]), float(center[1])]
            positions.append(normalized["center"])
        radius_m = shape.get("radius_m")
        if radius_m is not None:
            normalized["radius_m"] = float(radius_m)

        if normalized.get("bounds") or normalized.get("coordinates") or normalized.get("center"):
            normalized_shapes.append(normalized)

    center, zoom = _fit_center_zoom(positions)
    bounds = _bounds_from_positions(positions)

    features: list[dict[str, Any]] = []
    for marker in normalized_markers:
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": marker["position"]},
                "properties": {
                    "feature_kind": "marker",
                    "marker_id": marker["id"],
                    "label": marker["label"],
                    "color": marker["color"],
                },
            }
        )
    for route in normalized_routes:
        features.append(
            {
                "type": "Feature",
                "geometry": route["geometry"],
                "properties": {
                    "feature_kind": "route",
                    "route_id": route["id"],
                    "label": route["label"],
                    "color": route["color"],
                    "width": route["width"],
                },
            }
        )
    for shape in normalized_shapes:
        geometry: dict[str, Any] | None = None
        if shape["type"] == "rectangle" and isinstance(shape.get("bounds"), dict):
            southwest = shape["bounds"]["southwest"]
            northeast = shape["bounds"]["northeast"]
            west, south = southwest
            east, north = northeast
            ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
            geometry = {"type": "Polygon", "coordinates": [ring]}
        elif shape["type"] == "polygon" and isinstance(shape.get("coordinates"), list):
            ring = list(shape["coordinates"])
            if ring and ring[0] != ring[-1]:
                ring.append(ring[0])
            geometry = {"type": "Polygon", "coordinates": [ring]}
        elif shape["type"] == "circle" and isinstance(shape.get("center"), list):
            geometry = {"type": "Point", "coordinates": shape["center"]}
        if geometry:
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "feature_kind": "shape",
                        "shape_id": shape["id"],
                        "shape_type": shape["type"],
                        "label": shape["label"],
                        "color": shape["color"],
                        "fillColor": shape["fillColor"],
                        "fillOpacity": shape["fillOpacity"],
                        "weight": shape["weight"],
                        "radius_m": shape.get("radius_m"),
                    },
                }
            )

    return {
        "version": 1,
        "title": _safe_text(title) or "Map",
        "subtitle": _safe_text(subtitle) or None,
        "attribution": "© OpenStreetMap contributors",
        "view": {
            "center": center,
            "zoom": zoom,
            "bounds": bounds,
        },
        "markers": normalized_markers,
        "routes": normalized_routes,
        "shapes": normalized_shapes,
        "features": {
            "type": "FeatureCollection",
            "features": features,
        },
    }


def format_distance_meters(distance_m: float | None) -> str | None:
    if distance_m is None:
        return None
    try:
        value = float(distance_m)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value >= 1000:
        return f"{value / 1000.0:.1f} km"
    return f"{int(round(value))} m"


def format_duration_seconds(duration_s: float | None) -> str | None:
    if duration_s is None:
        return None
    try:
        value = float(duration_s)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    minutes = int(round(value / 60.0))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    if rem:
        return f"{hours} hr {rem} min"
    return f"{hours} hr"
