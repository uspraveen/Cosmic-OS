"""Rasterize SVG diagrams into a readable inline PNG preview.

Raw SVGs are kept as downloadable files. The preview is a screenshot window of
the diagram at a readable scale: small drawings are scaled up a little, and
long or wide drawings are cropped rather than compressed into a postage stamp.
"""

from __future__ import annotations

import io
import logging
import re
import tempfile
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

MIN_PREVIEW_SIDE_PX = 720
MAX_PREVIEW_WIDTH_PX = 1600
MAX_PREVIEW_HEIGHT_PX = 900
MAX_UPSCALE = 4.0
PREVIEW_PADDING_PX = 24
PREVIEW_BACKGROUND = "#0B1216"
PREVIEW_DEVICE_SCALE = 2
PREVIEW_SUFFIX = "_preview.png"

_XML_DECL_RE = re.compile(r"^\s*<\?xml[^?]*\?>\s*", re.IGNORECASE)


def preview_path_for(svg_path: Path) -> Path:
    return svg_path.with_name(f"{svg_path.stem}_preview.png")


def compute_preview_window(width: int, height: int) -> tuple[float, int, int]:
    """Return (scale, crop_width, crop_height) in CSS pixels of the source.

    Never scales down. Scales up only when the drawing is too small to read,
    and then only up to MAX_UPSCALE. Oversized diagrams keep native scale and
    are cropped to a top-left readable window.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    scale = 1.0
    min_side = min(width, height)
    if min_side < MIN_PREVIEW_SIDE_PX:
        scale = min(MIN_PREVIEW_SIDE_PX / float(min_side), MAX_UPSCALE)
    display_width = width * scale
    display_height = height * scale
    crop_width = max(1, min(int(round(display_width)), MAX_PREVIEW_WIDTH_PX))
    crop_height = max(1, min(int(round(display_height)), MAX_PREVIEW_HEIGHT_PX))
    return scale, crop_width, crop_height


def crop_readable_preview(png_bytes: bytes) -> bytes:
    """Scale-up tiny rasters, then crop a readable window. Never scale down."""
    with Image.open(io.BytesIO(png_bytes)) as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        scale, crop_width, crop_height = compute_preview_window(width, height)
        if scale > 1.01:
            rgba = rgba.resize(
                (
                    max(1, int(round(width * scale))),
                    max(1, int(round(height * scale))),
                ),
                Image.Resampling.LANCZOS,
            )
            width, height = rgba.size
        if width > crop_width or height > crop_height:
            rgba = rgba.crop((0, 0, min(width, crop_width), min(height, crop_height)))
        out = io.BytesIO()
        rgba.save(out, format="PNG")
        return out.getvalue()


def maybe_write_svg_preview(
    svg_path: Path,
    png_path: Path | None = None,
) -> Path | None:
    """Write a PNG preview next to `svg_path`. Returns the PNG path, or None."""
    resolved = Path(svg_path)
    if not resolved.is_file() or resolved.suffix.lower() != ".svg":
        return None
    destination = Path(png_path) if png_path is not None else preview_path_for(resolved)
    try:
        raw, already_windowed = _rasterize_svg(resolved)
        if not raw:
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw if already_windowed else crop_readable_preview(raw))
        if not destination.is_file() or destination.stat().st_size < 32:
            destination.unlink(missing_ok=True)
            return None
        return destination
    except Exception as exc:
        logger.warning("svg_preview.failed path=%s error=%s", resolved, exc)
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _rasterize_svg(svg_path: Path) -> tuple[bytes | None, bool]:
    png_bytes = _screenshot_with_playwright(svg_path)
    if png_bytes:
        return png_bytes, True
    fallback = _rasterize_with_svglib(svg_path)
    return fallback, False


def _wrap_svg_html(svg_markup: str) -> str:
    body = _XML_DECL_RE.sub("", svg_markup, count=1).strip()
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'/>"
        "<style>"
        f"html,body{{margin:0;padding:{PREVIEW_PADDING_PX}px;background:{PREVIEW_BACKGROUND};}}"
        "svg{display:block;}"
        "</style></head><body>"
        f"{body}"
        "</body></html>"
    )


def _launch_playwright_browser():
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    errors: list[str] = []
    for kwargs in (
        {"headless": True},
        {"headless": True, "channel": "msedge"},
        {"headless": True, "channel": "chrome"},
    ):
        try:
            browser = pw.chromium.launch(**kwargs)
            return pw, browser
        except Exception as exc:
            errors.append(str(exc))
    pw.stop()
    raise RuntimeError("Could not launch Chromium for SVG preview: " + " | ".join(errors))


def _screenshot_with_playwright(svg_path: Path) -> bytes | None:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return None

    try:
        markup = svg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "<svg" not in markup.lower():
        return None

    html = _wrap_svg_html(markup)
    pw = None
    browser = None
    try:
        pw, browser = _launch_playwright_browser()
        page = browser.new_page(device_scale_factor=PREVIEW_DEVICE_SCALE)
        page.set_viewport_size(
            {
                "width": MAX_PREVIEW_WIDTH_PX + (PREVIEW_PADDING_PX * 2),
                "height": MAX_PREVIEW_HEIGHT_PX + (PREVIEW_PADDING_PX * 2),
            }
        )
        with tempfile.TemporaryDirectory(prefix="svg_preview_") as tmpdir:
            html_path = Path(tmpdir) / "preview.html"
            html_path.write_text(html, encoding="utf-8")
            page.goto(html_path.as_uri(), wait_until="load", timeout=15000)
            svg = page.query_selector("svg")
            if svg is None:
                return None
            box = svg.bounding_box()
            if not box:
                return page.screenshot(type="png", animations="disabled")
            scale, crop_width, crop_height = compute_preview_window(
                int(round(box["width"])),
                int(round(box["height"])),
            )
            if scale > 1.01:
                page.evaluate(
                    """(value) => {
                        const svg = document.querySelector('svg');
                        if (!svg) return;
                        svg.style.transform = `scale(${value})`;
                        svg.style.transformOrigin = 'top left';
                    }""",
                    scale,
                )
            viewport_width = crop_width + (PREVIEW_PADDING_PX * 2)
            viewport_height = crop_height + (PREVIEW_PADDING_PX * 2)
            page.set_viewport_size(
                {"width": max(1, viewport_width), "height": max(1, viewport_height)}
            )
            return page.screenshot(type="png", animations="disabled")
    except Exception as exc:
        logger.info("svg_preview.playwright_unavailable error=%s", exc)
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass


def _rasterize_with_svglib(svg_path: Path) -> bytes | None:
    try:
        from reportlab.graphics import renderPM
        from svglib.svglib import svg2rlg
    except ImportError:
        return None
    try:
        drawing = svg2rlg(str(svg_path))
        if drawing is None:
            return None
        buf = io.BytesIO()
        renderPM.drawToFile(drawing, buf, fmt="PNG")
        payload = buf.getvalue()
        return payload or None
    except Exception as exc:
        logger.info("svg_preview.svglib_failed error=%s", exc)
        return None
