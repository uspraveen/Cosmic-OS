from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

_OUTPUT_WIDTH = 1600
_OUTPUT_HEIGHT = 900
_RENDER_SCALE = 1
_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def normalize_chart_spec(spec: dict[str, Any], *, max_points: int) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("Chart spec must be an object.")
    chart_type = str(spec.get("chart_type") or spec.get("type") or "").strip().lower()
    if chart_type not in {"bar", "line"}:
        raise ValueError("Supported chart types are bar and line.")
    title = str(spec.get("title") or "").strip() or "Chart"
    x_label = str(spec.get("x_label") or "").strip() or ""
    y_label = str(spec.get("y_label") or "").strip() or ""
    caption = str(spec.get("caption") or "").strip() or ""
    raw_series = spec.get("series")
    if not isinstance(raw_series, list) or not raw_series:
        raise ValueError("Chart spec requires a non-empty series list.")

    series: list[dict[str, Any]] = []
    total_points = 0
    for series_index, item in enumerate(raw_series, start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or f"Series {series_index}").strip()
        raw_points = item.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            continue
        points: list[dict[str, Any]] = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            x_value = str(point.get("x") or "").strip()
            try:
                y_value = float(point.get("y"))
            except (TypeError, ValueError):
                continue
            if not x_value:
                continue
            points.append({"x": x_value, "y": y_value})
        if not points:
            continue
        total_points += len(points)
        if total_points > max_points:
            raise ValueError(f"Chart exceeds the per-turn point budget of {max_points}.")
        series.append({"label": label, "points": points})

    if not series:
        raise ValueError("Chart spec did not contain any valid points.")

    return {
        "chart_type": chart_type,
        "title": title,
        "x_label": x_label or None,
        "y_label": y_label or None,
        "caption": caption or None,
        "series": series,
    }


@lru_cache(maxsize=None)
def _font_path(*, bold: bool = False) -> str | None:
    candidates = [
        Path("C:/Windows/Fonts/segoeui.ttf" if not bold else "C:/Windows/Fonts/segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf" if not bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf" if not bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(bold=bold)
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _format_tick(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B".rstrip("0").rstrip(".")
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K".rstrip("0").rstrip(".")
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _add_glow(
    image: Image.Image,
    *,
    bounds: tuple[int, int, int, int],
    color: str,
    alpha: int,
    blur_radius: int,
) -> Image.Image:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    rgba = ImageColor.getrgb(color) + (alpha,)
    draw.ellipse(bounds, fill=rgba)
    softened = overlay.filter(ImageFilter.GaussianBlur(blur_radius))
    return Image.alpha_composite(image, softened)


def _draw_chip(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    color: str,
    font: ImageFont.ImageFont,
    scale: int,
) -> int:
    text_width, text_height = _text_size(draw, text, font)
    chip_width = text_width + (34 * scale)
    chip_height = max(text_height + (14 * scale), 34 * scale)
    draw.rounded_rectangle(
        (x, y, x + chip_width, y + chip_height),
        radius=chip_height // 2,
        fill=(255, 255, 255, 18),
        outline=(255, 255, 255, 28),
        width=max(1, scale),
    )
    dot_y = y + (chip_height // 2)
    dot_x = x + (16 * scale)
    dot_radius = max(5 * scale, 8)
    draw.ellipse(
        (dot_x - dot_radius, dot_y - dot_radius, dot_x + dot_radius, dot_y + dot_radius),
        fill=ImageColor.getrgb(color) + (255,),
    )
    draw.text(
        (x + (30 * scale), y + ((chip_height - text_height) // 2) - (1 * scale)),
        text,
        font=font,
        fill="#edf2fb",
    )
    return chip_width


def _measure_chip_width(
    draw: ImageDraw.ImageDraw,
    *,
    text: str,
    font: ImageFont.ImageFont,
    scale: int,
) -> tuple[int, int]:
    text_width, text_height = _text_size(draw, text, font)
    chip_height = max(text_height + (14 * scale), 34 * scale)
    return text_width + (34 * scale), chip_height


def _build_background(width: int, height: int, scale: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), ImageColor.getrgb("#06080d"))
    image = _add_glow(
        image,
        bounds=(-int(width * 0.18), -int(height * 0.1), int(width * 0.44), int(height * 0.44)),
        color="#4cc9f0",
        alpha=68,
        blur_radius=120 * scale,
    )
    image = _add_glow(
        image,
        bounds=(int(width * 0.58), -int(height * 0.08), int(width * 1.08), int(height * 0.52)),
        color="#7f8cff",
        alpha=74,
        blur_radius=150 * scale,
    )
    image = _add_glow(
        image,
        bounds=(int(width * 0.18), int(height * 0.58), int(width * 0.68), int(height * 1.12)),
        color="#1b2432",
        alpha=210,
        blur_radius=140 * scale,
    )
    return image


def render_chart_png(spec: dict[str, Any]) -> bytes:
    output_width = _OUTPUT_WIDTH
    output_height = _OUTPUT_HEIGHT
    scale = _RENDER_SCALE
    width = output_width * scale
    height = output_height * scale
    image = _build_background(width, height, scale)
    draw = ImageDraw.Draw(image, "RGBA")

    title_font = _load_font(34 * scale, bold=True)
    subtitle_font = _load_font(15 * scale)
    label_font = _load_font(16 * scale, bold=True)
    tick_font = _load_font(14 * scale)
    legend_font = _load_font(13 * scale, bold=True)

    outer = (
        34 * scale,
        34 * scale,
        width - (34 * scale),
        height - (34 * scale),
    )
    draw.rounded_rectangle(
        outer,
        radius=28 * scale,
        fill=(10, 13, 20, 236),
        outline=(255, 255, 255, 26),
        width=max(1, 2 * scale),
    )
    draw.rounded_rectangle(
        (outer[0], outer[1], outer[2], outer[1] + (6 * scale)),
        radius=28 * scale,
        fill=(255, 255, 255, 16),
    )

    panel = (
        outer[0] + (28 * scale),
        outer[1] + (112 * scale),
        outer[2] - (28 * scale),
        outer[3] - (24 * scale),
    )
    draw.rounded_rectangle(
        panel,
        radius=24 * scale,
        fill=(255, 255, 255, 12),
        outline=(255, 255, 255, 24),
        width=max(1, scale),
    )

    plot_left = panel[0] + (108 * scale)
    plot_right = panel[2] - (52 * scale)
    plot_top = panel[1] + (86 * scale)
    plot_bottom = panel[3] - (112 * scale)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    ordered_categories: list[str] = []
    seen_categories: set[str] = set()
    y_values: list[float] = []
    for series in spec["series"]:
        for point in series["points"]:
            category = point["x"]
            if category not in seen_categories:
                seen_categories.add(category)
                ordered_categories.append(category)
            y_values.append(float(point["y"]))
    if not ordered_categories or not y_values:
        raise ValueError("Chart spec did not contain drawable data.")

    min_y = min(0.0, min(y_values))
    max_y = max(0.0, max(y_values))
    if min_y == max_y:
        max_y = min_y + 1.0
    value_padding = (max_y - min_y) * 0.12
    min_y -= value_padding
    max_y += value_padding
    if min_y == max_y:
        max_y = min_y + 1.0

    def y_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_bottom - (ratio * plot_height)

    axis_zero_y = y_to_px(0.0)
    grid_lines = 5
    for idx in range(grid_lines + 1):
        value = min_y + ((max_y - min_y) * (idx / grid_lines))
        y = y_to_px(value)
        draw.line((plot_left, y, plot_right, y), fill=(255, 255, 255, 16), width=max(1, scale))
        tick_text = _format_tick(value)
        tick_width, tick_height = _text_size(draw, tick_text, tick_font)
        draw.text(
            (plot_left - tick_width - (16 * scale), y - (tick_height // 2)),
            tick_text,
            font=tick_font,
            fill="#90a0b8",
        )

    category_count = len(ordered_categories)
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(ordered_categories)
    }
    for idx in range(category_count + 1):
        x = plot_left + (idx * category_step)
        draw.line((x, plot_top, x, plot_bottom), fill=(255, 255, 255, 10), width=max(1, scale))

    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=(255, 255, 255, 42), width=max(1, 2 * scale))
    draw.line((plot_left, axis_zero_y, plot_right, axis_zero_y), fill=(255, 255, 255, 42), width=max(1, 2 * scale))

    palette = ["#f4f7fb", "#9dcbff", "#7ce0d4", "#9d9fb2"]

    if spec["chart_type"] == "bar":
        series_count = len(spec["series"])
        group_width = category_step * 0.72
        bar_width = group_width / max(series_count, 1)
        for series_index, series in enumerate(spec["series"]):
            color = palette[series_index % len(palette)]
            rgba = ImageColor.getrgb(color)
            for category in ordered_categories:
                point = next((item for item in series["points"] if item["x"] == category), None)
                if point is None:
                    continue
                center = category_centers[category]
                left = int(center - (group_width / 2) + (series_index * bar_width) + (6 * scale))
                right = int(left + max(22 * scale, bar_width - (12 * scale)))
                top = int(y_to_px(float(point["y"])))
                bottom = int(axis_zero_y)
                if top > bottom:
                    top, bottom = bottom, top
                draw.rounded_rectangle(
                    (left, top, right, bottom),
                    radius=10 * scale,
                    fill=rgba + (214,),
                    outline=rgba + (255,),
                    width=max(1, scale),
                )
                draw.rounded_rectangle(
                    (left, top, right, min(bottom, top + (18 * scale))),
                    radius=10 * scale,
                    fill=(255, 255, 255, 34),
                )
    else:
        for series_index, series in enumerate(spec["series"]):
            color = palette[series_index % len(palette)]
            rgba = ImageColor.getrgb(color)
            polyline: list[tuple[float, float]] = []
            for point in series["points"]:
                polyline.append((category_centers[point["x"]], y_to_px(float(point["y"]))))
            if len(polyline) >= 2:
                if series_index == 0:
                    polygon = [(polyline[0][0], axis_zero_y)] + polyline + [(polyline[-1][0], axis_zero_y)]
                    draw.polygon(polygon, fill=rgba + (52,))
                draw.line(polyline, fill=rgba + (255,), width=max(6 * scale, 8), joint="curve")
            for x, y in polyline:
                halo = 8 * scale
                draw.ellipse((x - halo, y - halo, x + halo, y + halo), fill=rgba + (70,))
                dot = 5 * scale
                draw.ellipse(
                    (x - dot, y - dot, x + dot, y + dot),
                    fill=rgba + (255,),
                    outline=(8, 10, 18, 255),
                    width=max(2 * scale, 2),
                )

    for idx, category in enumerate(ordered_categories):
        center = int(plot_left + (idx + 0.5) * category_step)
        text_width, text_height = _text_size(draw, category, tick_font)
        draw.text(
            (center - (text_width // 2), plot_bottom + (16 * scale)),
            category,
            font=tick_font,
            fill="#d4dce8",
        )

    if spec.get("x_label"):
        x_label = str(spec["x_label"])
        x_width, x_height = _text_size(draw, x_label, label_font)
        draw.text(
            (plot_left + (plot_width // 2) - (x_width // 2), panel[3] - (52 * scale)),
            x_label,
            font=label_font,
            fill="#edf2fb",
        )
    if spec.get("y_label"):
        draw.text(
            (panel[0] + (22 * scale), panel[1] + (22 * scale)),
            str(spec["y_label"]),
            font=label_font,
            fill="#edf2fb",
        )

    draw.text((outer[0] + (34 * scale), outer[1] + (24 * scale)), spec["title"], font=title_font, fill="#f8fbff")
    subtitle = "Inline chart generated from structured data in the response"
    draw.text(
        (outer[0] + (34 * scale), outer[1] + (72 * scale)),
        subtitle,
        font=subtitle_font,
        fill="#97a5ba",
    )

    chip_x = outer[2] - (34 * scale)
    chip_y = outer[1] + (30 * scale)
    for series_index, series in reversed(list(enumerate(spec["series"]))):
        label = str(series["label"])
        chip_width, _ = _measure_chip_width(
            draw,
            text=label,
            font=legend_font,
            scale=scale,
        )
        chip_x -= chip_width
        _draw_chip(
            draw,
            x=chip_x,
            y=chip_y,
            text=label,
            color=palette[series_index % len(palette)],
            font=legend_font,
            scale=scale,
        )
        chip_x -= 10 * scale

    final_image = image.resize((output_width, output_height), _LANCZOS)
    buffer = BytesIO()
    final_image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
