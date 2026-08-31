from __future__ import annotations

from shared.response_blocks import build_response_blocks


def test_build_response_blocks_splits_markdown_code_and_appends_artifacts() -> None:
    blocks = build_response_blocks(
        "Intro\n\n```python\nprint('hi')\n```\n\nDone",
        [
            {
                "artifact_id": "art_plot",
                "filename": "plot.png",
                "mime_type": "image/png",
                "size_bytes": 2048,
                "downloadable": True,
            }
        ],
    )

    assert [block["type"] for block in blocks] == ["markdown", "code", "markdown", "image_artifact"]
    assert blocks[0]["text"] == "Intro\n\n"
    assert blocks[1]["language"] == "python"
    assert blocks[1]["code"] == "print('hi')\n"
    assert blocks[2]["text"] == "\n\nDone"
    assert blocks[3]["artifact_id"] == "art_plot"
    assert blocks[3]["filename"] == "plot.png"


def test_build_response_blocks_keeps_svg_as_file_and_png_preview_as_image() -> None:
    blocks = build_response_blocks(
        "Diagram ready.",
        [
            {
                "artifact_id": "art_preview",
                "filename": "diagram_preview.png",
                "mime_type": "image/png",
                "kind": "output",
            },
            {
                "artifact_id": "art_svg",
                "filename": "diagram.svg",
                "mime_type": "image/svg+xml",
                "kind": "output",
            },
        ],
    )

    assert [block["type"] for block in blocks] == [
        "markdown",
        "image_artifact",
        "file_artifact",
    ]
    assert blocks[1]["filename"] == "diagram_preview.png"
    assert blocks[2]["filename"] == "diagram.svg"


def test_build_response_blocks_places_artifact_marker_inline() -> None:
    blocks = build_response_blocks(
        "Here is the chart:\n\n[[artifact:plot.png]]\n\nAfter the figure.",
        [
            {
                "artifact_id": "art_plot",
                "filename": "plot.png",
                "mime_type": "image/png",
                "downloadable": True,
            }
        ],
    )

    assert [block["type"] for block in blocks] == ["markdown", "image_artifact", "markdown"]
    assert blocks[0]["text"] == "Here is the chart:\n\n"
    assert blocks[1]["artifact_id"] == "art_plot"
    assert blocks[2]["text"] == "\n\nAfter the figure."


def test_build_response_blocks_places_map_artifact_marker_inline() -> None:
    blocks = build_response_blocks(
        "Here is the route:\n\n[[artifact:map.cosmic-map.json]]\n",
        [
            {
                "artifact_id": "art_map",
                "filename": "map.cosmic-map.json",
                "mime_type": "application/vnd.cosmic.map+json",
                "kind": "map",
            }
        ],
    )

    assert [block["type"] for block in blocks] == ["markdown", "map_artifact", "markdown"]
    assert blocks[1]["artifact_id"] == "art_map"
