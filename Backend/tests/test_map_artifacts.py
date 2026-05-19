from __future__ import annotations

from shared.map_artifacts import is_supported_map_artifact
from shared.response_blocks import build_response_blocks


def test_is_supported_map_artifact() -> None:
    assert is_supported_map_artifact({"kind": "map", "filename": "map.cosmic-map.json"})
    assert is_supported_map_artifact(
        {"mime_type": "application/vnd.cosmic.map+json", "filename": "route.json"}
    )
    assert not is_supported_map_artifact({"kind": "chart", "filename": "plot.png"})


def test_build_response_blocks_emits_map_artifact() -> None:
    blocks = build_response_blocks(
        "Route ready.\n\n[[artifact:map.cosmic-map.json]]\n",
        [
            {
                "artifact_id": "art_map_1",
                "filename": "map.cosmic-map.json",
                "mime_type": "application/vnd.cosmic.map+json",
                "kind": "map",
            }
        ],
    )
    assert [block["type"] for block in blocks] == ["markdown", "map_artifact", "markdown"]
    assert blocks[1]["artifact_id"] == "art_map_1"
