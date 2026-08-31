from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from shared.svg_preview import (
    MAX_PREVIEW_HEIGHT_PX,
    MAX_PREVIEW_WIDTH_PX,
    compute_preview_window,
    crop_readable_preview,
    maybe_write_svg_preview,
    preview_path_for,
)


def _png_bytes(width: int, height: int, color: str = "#2596be") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_preview_path_for_uses_preview_suffix() -> None:
    assert preview_path_for(Path("runs/diagram.svg")) == Path("runs/diagram_preview.png")


def test_compute_preview_window_does_not_scale_down_large_diagrams() -> None:
    scale, crop_width, crop_height = compute_preview_window(4000, 2200)
    assert scale == 1.0
    assert crop_width == MAX_PREVIEW_WIDTH_PX
    assert crop_height == MAX_PREVIEW_HEIGHT_PX


def test_compute_preview_window_scales_up_small_diagrams() -> None:
    scale, crop_width, crop_height = compute_preview_window(200, 160)
    assert scale == 4.0
    assert (crop_width, crop_height) == (800, 640)


def test_crop_readable_preview_keeps_a_window_of_wide_diagrams() -> None:
    cropped = crop_readable_preview(_png_bytes(4000, 400, "#123456"))
    with Image.open(io.BytesIO(cropped)) as image:
        width, height = image.size
    assert width <= MAX_PREVIEW_WIDTH_PX
    assert height <= MAX_PREVIEW_HEIGHT_PX
    assert width >= 700
    assert height >= 400


def test_crop_readable_preview_does_not_shrink_oversized_diagrams_to_fit() -> None:
    cropped = crop_readable_preview(_png_bytes(3200, 1800))
    with Image.open(io.BytesIO(cropped)) as image:
        assert image.size == (MAX_PREVIEW_WIDTH_PX, MAX_PREVIEW_HEIGHT_PX)


def test_maybe_write_svg_preview_ignores_non_svg(tmp_path: Path) -> None:
    png_path = tmp_path / "plot.png"
    png_path.write_bytes(_png_bytes(8, 8))
    assert maybe_write_svg_preview(png_path) is None


def test_maybe_write_svg_preview_writes_png_when_rasterizer_available(
    tmp_path: Path,
) -> None:
    svg_path = tmp_path / "diagram.svg"
    svg_path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="240">'
        '<rect width="400" height="240" fill="#2596be"/>'
        "</svg>",
        encoding="utf-8",
    )
    preview = maybe_write_svg_preview(svg_path)
    if preview is None:
        import pytest

        pytest.skip("SVG rasterizer (Playwright or svglib) is unavailable")
    assert preview == tmp_path / "diagram_preview.png"
    assert preview.is_file()
    with Image.open(preview) as image:
        assert image.size[0] >= 200
        assert image.size[1] >= 200
        assert image.size[0] <= MAX_PREVIEW_WIDTH_PX * 3
        assert image.size[1] <= MAX_PREVIEW_HEIGHT_PX * 3
