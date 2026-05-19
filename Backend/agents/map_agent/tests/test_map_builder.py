from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.map_agent.map_builder import build_map_spec, format_distance_meters, format_duration_seconds


def test_build_map_spec_includes_markers_and_route() -> None:
    spec = build_map_spec(
        title="Test route",
        markers=[
            {"label": "A", "position": [2.0, 48.0]},
            {"label": "B", "position": [2.5, 48.5]},
        ],
        routes=[
            {
                "label": "Main",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[2.0, 48.0], [2.25, 48.25], [2.5, 48.5]],
                },
                "distance_m": 1500,
                "duration_s": 240,
            }
        ],
    )

    assert spec["title"] == "Test route"
    assert len(spec["markers"]) == 2
    assert len(spec["routes"]) == 1
    assert spec["features"]["type"] == "FeatureCollection"
    assert len(spec["features"]["features"]) == 3
    assert spec["view"]["center"][0] == 2.25


def test_format_distance_and_duration() -> None:
    assert format_distance_meters(850) == "850 m"
    assert format_distance_meters(2400) == "2.4 km"
    assert format_duration_seconds(75) == "1 min"
    assert format_duration_seconds(3900) == "1 hr 5 min"
