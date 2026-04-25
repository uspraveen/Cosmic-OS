from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont

_OUTPUT_WIDTH = 1600
_OUTPUT_HEIGHT = 900
_RENDER_SCALE = 1
_LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
_SERIES_PALETTE = ("#0A84FF", "#30D158", "#E8EDF7", "#BF5AF2", "#FF9F0A")


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
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            if not bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        ),
        Path(
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"
            if not bold
            else "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
        ),
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


def _blur_overlay(overlay: Image.Image, blur_radius: int) -> Image.Image:
    if blur_radius <= 0:
        return overlay
    return overlay.filter(ImageFilter.GaussianBlur(blur_radius))


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
    softened = _blur_overlay(overlay, blur_radius)
    return Image.alpha_composite(image, softened)


def _add_vertical_gradient(
    image: Image.Image,
    *,
    top_color: str,
    bottom_color: str,
    alpha: int,
) -> Image.Image:
    width, height = image.size
    top = ImageColor.getrgb(top_color)
    bottom = ImageColor.getrgb(bottom_color)
    gradient = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(gradient, "RGBA")
    for y in range(height):
        ratio = y / max(height - 1, 1)
        row = tuple(int(top[channel] + ((bottom[channel] - top[channel]) * ratio)) for channel in range(3))
        draw.line((0, y, width, y), fill=row + (alpha,))
    return Image.alpha_composite(image, gradient)


def _draw_glass_band(
    image: Image.Image,
    *,
    points: list[tuple[int, int]],
    fill: tuple[int, int, int, int],
    blur_radius: int,
) -> Image.Image:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.polygon(points, fill=fill)
    return Image.alpha_composite(image, overlay)


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
    chip_height = max(text_height + (16 * scale), 36 * scale)
    chip_width = text_width + (40 * scale)
    draw.rounded_rectangle(
        (x, y, x + chip_width, y + chip_height),
        radius=chip_height // 2,
        fill=(255, 255, 255, 18),
        outline=(255, 255, 255, 24),
        width=max(1, scale),
    )
    dot_size = 10 * scale
    dot_x = x + (16 * scale)
    dot_y = y + (chip_height // 2)
    draw.ellipse(
        (dot_x - dot_size // 2, dot_y - dot_size // 2, dot_x + dot_size // 2, dot_y + dot_size // 2),
        fill=ImageColor.getrgb(color) + (255,),
    )
    draw.text(
        (x + (30 * scale), y + ((chip_height - text_height) // 2) - (1 * scale)),
        text,
        font=font,
        fill="#eef3ff",
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
    chip_height = max(text_height + (16 * scale), 36 * scale)
    return text_width + (40 * scale), chip_height


def _draw_panel(
    image: Image.Image,
    *,
    bounds: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int, int],
    outline: tuple[int, int, int, int],
    highlight_alpha: int,
) -> Image.Image:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    shadow_offset = max(10, radius // 2)
    shadow_bounds = (
        bounds[0],
        bounds[1] + shadow_offset,
        bounds[2],
        bounds[3] + shadow_offset,
    )
    shadow_draw.rounded_rectangle(shadow_bounds, radius=radius, fill=(0, 0, 0, 58))
    image = Image.alpha_composite(image, shadow)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=outline, width=max(1, radius // 14))
    top_strip = (bounds[0] + radius // 3, bounds[1] + radius // 3, bounds[2] - radius // 3, bounds[1] + radius)
    draw.rounded_rectangle(top_strip, radius=radius // 2, fill=(255, 255, 255, highlight_alpha))
    draw.arc(
        (bounds[0] + radius // 3, bounds[1] + radius // 4, bounds[2] - radius // 3, bounds[3] - radius // 4),
        start=205,
        end=335,
        fill=(255, 255, 255, max(12, highlight_alpha)),
        width=max(1, radius // 18),
    )
    return Image.alpha_composite(image, overlay)


def _draw_gradient_rounded_rect(
    image: Image.Image,
    *,
    bounds: tuple[int, int, int, int],
    radius: int,
    top_color: str,
    bottom_color: str,
    alpha: int,
    outline: tuple[int, int, int, int] | None = None,
    width: int = 1,
) -> Image.Image:
    left, top, right, bottom = bounds
    rect_width = max(1, right - left)
    rect_height = max(1, bottom - top)
    top_rgb = ImageColor.getrgb(top_color)
    bottom_rgb = ImageColor.getrgb(bottom_color)
    gradient = Image.new("RGBA", (rect_width, rect_height), (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient, "RGBA")
    for y in range(rect_height):
        ratio = y / max(rect_height - 1, 1)
        color = tuple(int(top_rgb[channel] + ((bottom_rgb[channel] - top_rgb[channel]) * ratio)) for channel in range(3))
        gradient_draw.line((0, y, rect_width, y), fill=color + (alpha,))
    mask = Image.new("L", (rect_width, rect_height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, rect_width - 1, rect_height - 1), radius=radius, fill=255)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay.paste(gradient, (left, top), mask)
    if outline:
        outline_draw = ImageDraw.Draw(overlay, "RGBA")
        outline_draw.rounded_rectangle(bounds, radius=radius, outline=outline, width=max(1, width))
    return Image.alpha_composite(image, overlay)


def _draw_callout(
    draw: ImageDraw.ImageDraw,
    *,
    anchor_x: float,
    anchor_y: float,
    text: str,
    font: ImageFont.ImageFont,
    scale: int,
) -> None:
    text_width, text_height = _text_size(draw, text, font)
    bubble_width = text_width + (30 * scale)
    bubble_height = text_height + (18 * scale)
    bubble_x = int(anchor_x - (bubble_width // 2))
    bubble_y = int(anchor_y - bubble_height - (22 * scale))
    draw.rounded_rectangle(
        (bubble_x, bubble_y, bubble_x + bubble_width, bubble_y + bubble_height),
        radius=16 * scale,
        fill=(238, 244, 255, 238),
        outline=(255, 255, 255, 84),
        width=max(1, scale),
    )
    draw.text(
        (bubble_x + (bubble_width - text_width) // 2, bubble_y + (bubble_height - text_height) // 2 - (1 * scale)),
        text,
        font=font,
        fill="#070A10",
    )
    draw.line(
        (
            int(anchor_x),
            bubble_y + bubble_height,
            int(anchor_x),
            int(anchor_y - (7 * scale)),
        ),
        fill=(246, 248, 255, 210),
        width=max(2 * scale, 2),
    )


def _build_background(width: int, height: int, scale: int) -> Image.Image:
    image = Image.new("RGBA", (width, height), ImageColor.getrgb("#03050a"))
    image = _add_vertical_gradient(
        image,
        top_color="#121722",
        bottom_color="#04060b",
        alpha=225,
    )
    image = _add_glow(
        image,
        bounds=(-int(width * 0.18), -int(height * 0.16), int(width * 0.48), int(height * 0.5)),
        color="#0A84FF",
        alpha=88,
        blur_radius=210 * scale,
    )
    image = _add_glow(
        image,
        bounds=(int(width * 0.52), -int(height * 0.14), int(width * 1.1), int(height * 0.5)),
        color="#30D158",
        alpha=52,
        blur_radius=230 * scale,
    )
    image = _add_glow(
        image,
        bounds=(int(width * 0.26), int(height * 0.46), int(width * 0.86), int(height * 1.08)),
        color="#BF5AF2",
        alpha=38,
        blur_radius=210 * scale,
    )
    image = _draw_glass_band(
        image,
        points=[
            (int(width * -0.1), int(height * 0.18)),
            (int(width * 1.06), int(height * -0.08)),
            (int(width * 1.15), int(height * 0.04)),
            (int(width * 0.0), int(height * 0.31)),
        ],
        fill=(255, 255, 255, 17),
        blur_radius=18 * scale,
    )
    image = _draw_glass_band(
        image,
        points=[
            (int(width * 0.04), int(height * 0.96)),
            (int(width * 1.08), int(height * 0.56)),
            (int(width * 1.14), int(height * 0.76)),
            (int(width * 0.22), int(height * 1.08)),
        ],
        fill=(255, 255, 255, 11),
        blur_radius=24 * scale,
    )
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    shadow_draw.rounded_rectangle(
        (int(width * 0.06), int(height * 0.08), int(width * 0.94), int(height * 0.92)),
        radius=64 * scale,
        fill=(0, 0, 0, 78),
    )
    return Image.alpha_composite(image, shadow)


def _render_chart_png_legacy(spec: dict[str, Any]) -> bytes:
    output_width = _OUTPUT_WIDTH
    output_height = _OUTPUT_HEIGHT
    scale = _RENDER_SCALE
    width = output_width * scale
    height = output_height * scale

    image = _build_background(width, height, scale)
    image = _draw_panel(
        image,
        bounds=(48 * scale, 42 * scale, width - (48 * scale), height - (42 * scale)),
        radius=40 * scale,
        fill=(9, 12, 18, 226),
        outline=(255, 255, 255, 34),
        highlight_alpha=28,
    )
    image = _draw_panel(
        image,
        bounds=(92 * scale, 196 * scale, width - (92 * scale), height - (84 * scale)),
        radius=30 * scale,
        fill=(14, 17, 25, 204),
        outline=(255, 255, 255, 24),
        highlight_alpha=18,
    )
    draw = ImageDraw.Draw(image, "RGBA")

    eyebrow_font = _load_font(13 * scale, bold=True)
    title_font = _load_font(35 * scale, bold=True)
    subtitle_font = _load_font(16 * scale)
    label_font = _load_font(14 * scale, bold=True)
    tick_font = _load_font(15 * scale)
    chip_font = _load_font(13 * scale, bold=True)
    callout_font = _load_font(15 * scale, bold=True)

    outer = (
        48 * scale,
        42 * scale,
        width - (48 * scale),
        height - (42 * scale),
    )
    panel = (
        92 * scale,
        196 * scale,
        width - (92 * scale),
        height - (84 * scale),
    )
    plot_left = panel[0] + (124 * scale)
    plot_right = panel[2] - (70 * scale)
    plot_top = panel[1] + (82 * scale)
    plot_bottom = panel[3] - (120 * scale)
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
    value_padding = (max_y - min_y) * 0.16
    min_y -= value_padding
    max_y += value_padding
    if min_y == max_y:
        max_y = min_y + 1.0

    def y_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_bottom - (ratio * plot_height)

    axis_zero_y = y_to_px(0.0)
    category_count = len(ordered_categories)
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(ordered_categories)
    }

    plot_shell = (
        plot_left - (34 * scale),
        plot_top - (38 * scale),
        plot_right + (24 * scale),
        plot_bottom + (64 * scale),
    )
    draw.rounded_rectangle(
        plot_shell,
        radius=28 * scale,
        fill=(255, 255, 255, 8),
        outline=(255, 255, 255, 16),
        width=max(1, scale),
    )

    draw.text(
        (outer[0] + (44 * scale), outer[1] + (34 * scale)),
        "COSMIC VISUAL",
        font=eyebrow_font,
        fill="#8EA0B8",
    )
    draw.text(
        (outer[0] + (44 * scale), outer[1] + (64 * scale)),
        spec["title"],
        font=title_font,
        fill="#f6f8ff",
    )
    accent_y = outer[1] + (110 * scale)
    accent_left = outer[0] + (44 * scale)
    accent_right = accent_left + (210 * scale)
    for offset in range(5 * scale):
        ratio = offset / max((5 * scale) - 1, 1)
        alpha = int(110 * (1 - ratio))
        draw.line(
            (accent_left, accent_y + offset, accent_right, accent_y + offset),
            fill=ImageColor.getrgb("#30D158") + (alpha,),
            width=max(1, scale),
        )
    subtitle = str(spec.get("caption") or "").strip() or "Generated from structured data in this response."
    draw.text(
        (outer[0] + (44 * scale), outer[1] + (116 * scale)),
        subtitle,
        font=subtitle_font,
        fill="#A2ADBE",
    )

    chip_x = outer[2] - (46 * scale)
    chip_y = outer[1] + (58 * scale)
    for series_index, series in reversed(list(enumerate(spec["series"]))):
        chip_width, _ = _measure_chip_width(
            draw,
            text=str(series["label"]),
            font=chip_font,
            scale=scale,
        )
        chip_x -= chip_width
        _draw_chip(
            draw,
            x=chip_x,
            y=chip_y,
            text=str(series["label"]),
            color=_SERIES_PALETTE[series_index % len(_SERIES_PALETTE)],
            font=chip_font,
            scale=scale,
        )
        chip_x -= 12 * scale

    grid_lines = 5
    for idx in range(grid_lines + 1):
        value = min_y + ((max_y - min_y) * (idx / grid_lines))
        y = y_to_px(value)
        is_zero = abs(value) <= max(abs(max_y - min_y) * 0.02, 1e-9)
        draw.line(
            (plot_left, y, plot_right, y),
            fill=(255, 255, 255, 34 if is_zero else 15),
            width=max(1, 2 * scale if is_zero else scale),
        )
        tick_text = _format_tick(value)
        tick_width, tick_height = _text_size(draw, tick_text, tick_font)
        draw.text(
            (plot_left - tick_width - (20 * scale), y - (tick_height // 2)),
            tick_text,
            font=tick_font,
            fill="#8F9BAE",
        )

    for idx in range(category_count):
        x = plot_left + (idx * category_step)
        draw.line(
            (x, plot_top, x, plot_bottom),
            fill=(255, 255, 255, 8),
            width=max(1, scale),
        )

    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=(255, 255, 255, 22), width=max(1, scale))
    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=(255, 255, 255, 22), width=max(1, scale))

    if spec["chart_type"] == "bar":
        series_count = len(spec["series"])
        group_width = category_step * 0.68
        bar_width = group_width / max(series_count, 1)
        palette = ("#E8EDF7", "#0A84FF", "#30D158", "#BF5AF2", "#FF9F0A")
        for series_index, series in enumerate(spec["series"]):
            color = palette[series_index % len(palette)]
            base_rgb = ImageColor.getrgb(color)
            for category in ordered_categories:
                point = next((item for item in series["points"] if item["x"] == category), None)
                if point is None:
                    continue
                center = category_centers[category]
                left = int(center - (group_width / 2) + (series_index * bar_width) + (8 * scale))
                right = int(left + max(24 * scale, bar_width - (14 * scale)))
                top = int(y_to_px(float(point["y"])))
                bottom = int(axis_zero_y)
                if top > bottom:
                    top, bottom = bottom, top
                glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
                glow_draw = ImageDraw.Draw(glow, "RGBA")
                glow_draw.rounded_rectangle(
                    (left - (4 * scale), top - (4 * scale), right + (4 * scale), bottom + (4 * scale)),
                    radius=13 * scale,
                    fill=base_rgb + (48,),
                )
                image = Image.alpha_composite(image, _blur_overlay(glow, 12 * scale))
                draw = ImageDraw.Draw(image, "RGBA")
                image = _draw_gradient_rounded_rect(
                    image,
                    bounds=(left, top, right, bottom),
                    radius=10 * scale,
                    top_color="#F8FBFF" if series_index == 0 else color,
                    bottom_color=color if series_index == 0 else "#0A1020",
                    alpha=232,
                    outline=(255, 255, 255, 32),
                    width=max(1, scale),
                )
                draw = ImageDraw.Draw(image, "RGBA")
                draw.rounded_rectangle(
                    (left + (3 * scale), top + (3 * scale), right - (3 * scale), min(bottom, top + (15 * scale))),
                    radius=8 * scale,
                    fill=(255, 255, 255, 42),
                )
    else:
        primary_peak: tuple[float, float, str] | None = None
        for series_index, series in enumerate(spec["series"]):
            color = _SERIES_PALETTE[series_index % len(_SERIES_PALETTE)]
            rgba = ImageColor.getrgb(color)
            polyline: list[tuple[float, float]] = []
            for point in series["points"]:
                polyline.append((category_centers[point["x"]], y_to_px(float(point["y"]))))
            if len(polyline) >= 2:
                if series_index == 0:
                    polygon = [(polyline[0][0], plot_bottom)] + polyline + [(polyline[-1][0], plot_bottom)]
                    draw.polygon(polygon, fill=rgba + (34,))
                glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
                glow_draw = ImageDraw.Draw(glow, "RGBA")
                glow_draw.line(polyline, fill=rgba + (118,), width=max(18 * scale, 18), joint="curve")
                image = Image.alpha_composite(image, _blur_overlay(glow, 9 * scale))
                draw = ImageDraw.Draw(image, "RGBA")
                draw.line(polyline, fill=rgba + (248,), width=max(6 * scale, 7), joint="curve")
                draw.line(polyline, fill=(255, 255, 255, 72), width=max(2 * scale, 2), joint="curve")
            for point_index, (x, y) in enumerate(polyline):
                halo = 10 * scale
                draw.ellipse((x - halo, y - halo, x + halo, y + halo), fill=rgba + (62,))
                dot = 5 * scale
                draw.ellipse(
                    (x - dot, y - dot, x + dot, y + dot),
                    fill=(245, 249, 255, 255),
                    outline=(10, 12, 18, 255),
                    width=max(2 * scale, 2),
                )
                if series_index == 0:
                    point_value = float(series["points"][point_index]["y"])
                    if primary_peak is None or point_value > float(primary_peak[2]):
                        primary_peak = (x, y, str(point_value))
        if primary_peak is not None:
            _draw_callout(
                draw,
                anchor_x=primary_peak[0],
                anchor_y=primary_peak[1],
                text=_format_tick(float(primary_peak[2])),
                font=callout_font,
                scale=scale,
            )

    for idx, category in enumerate(ordered_categories):
        center = int(plot_left + (idx + 0.5) * category_step)
        text_width, text_height = _text_size(draw, category, tick_font)
        draw.text(
            (center - (text_width // 2), plot_bottom + (16 * scale)),
            category,
            font=tick_font,
            fill="#d4dcea",
        )

    if spec.get("x_label"):
        x_label = str(spec["x_label"])
        x_width, x_height = _text_size(draw, x_label, label_font)
        draw.text(
            (plot_left + (plot_width // 2) - (x_width // 2), panel[3] - (58 * scale)),
            x_label,
            font=label_font,
            fill="#e7edf8",
        )
    if spec.get("y_label"):
        draw.text(
            (panel[0] + (28 * scale), panel[1] + (18 * scale)),
            str(spec["y_label"]),
            font=label_font,
            fill="#e7edf8",
        )

    final_image = image.resize((output_width, output_height), _LANCZOS).convert("RGB")
    buffer = BytesIO()
    final_image.save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()


def _trim_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    if _text_size(draw, value, font)[0] <= max_width:
        return value
    ellipsis = "..."
    while value and _text_size(draw, f"{value}{ellipsis}", font)[0] > max_width:
        value = value[:-1].rstrip()
    return f"{value}{ellipsis}" if value else ellipsis


def _draw_text_block(
    draw: ImageDraw.ImageDraw,
    *,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    max_width: int,
    line_gap: int,
    max_lines: int,
) -> int:
    words = str(text or "").strip().split()
    if not words:
        return xy[1]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_size(draw, candidate, font)[0] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        lines[-1] = _trim_to_width(draw, lines[-1], font=font, max_width=max_width)

    y = xy[1]
    for line in lines:
        draw.text((xy[0], y), line, font=font, fill=fill)
        y += _text_size(draw, line, font)[1] + line_gap
    return y


def _draw_simple_chip(
    draw: ImageDraw.ImageDraw,
    *,
    x: int,
    y: int,
    text: str,
    color: str,
    font: ImageFont.ImageFont,
) -> int:
    label = str(text or "").strip()
    text_width, text_height = _text_size(draw, label, font)
    width = text_width + 44
    height = max(34, text_height + 14)
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=height // 2,
        fill=(255, 255, 255, 10),
        outline=(255, 255, 255, 22),
        width=1,
    )
    draw.ellipse((x + 14, y + height // 2 - 5, x + 24, y + height // 2 + 5), fill=ImageColor.getrgb(color))
    draw.text((x + 32, y + (height - text_height) // 2 - 1), label, font=font, fill="#E8EEF8")
    return width


def _draw_bar(
    draw: ImageDraw.ImageDraw,
    *,
    bounds: tuple[int, int, int, int],
    color: str,
) -> None:
    left, top, right, bottom = bounds
    if bottom - top < 2:
        top = bottom - 2
    radius = min(16, max(4, (right - left) // 3))
    rgb = ImageColor.getrgb(color)
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=rgb + (228,))
    draw.rounded_rectangle(
        (left + 2, top + 2, right - 2, min(bottom, top + 14)),
        radius=max(3, radius - 2),
        fill=(255, 255, 255, 44),
    )
    draw.line((left, bottom, right, bottom), fill=(255, 255, 255, 56), width=1)


def _nice_range(values: list[float]) -> tuple[float, float]:
    min_y = min(0.0, min(values))
    max_y = max(0.0, max(values))
    if min_y == max_y:
        max_y = min_y + 1.0
    padding = (max_y - min_y) * 0.12
    return min_y - padding, max_y + padding


def render_chart_png(spec: dict[str, Any]) -> bytes:
    output_width = _OUTPUT_WIDTH
    output_height = _OUTPUT_HEIGHT
    image = Image.new("RGBA", (output_width, output_height), ImageColor.getrgb("#050505") + (255,))
    image = _add_vertical_gradient(image, top_color="#10131A", bottom_color="#050505", alpha=190)
    image = _add_glow(
        image,
        bounds=(-220, -180, 640, 520),
        color="#0A84FF",
        alpha=54,
        blur_radius=180,
    )
    image = _add_glow(
        image,
        bounds=(940, -140, 1780, 480),
        color="#30D158",
        alpha=34,
        blur_radius=210,
    )
    image = _add_glow(
        image,
        bounds=(430, 550, 1120, 1120),
        color="#BF5AF2",
        alpha=25,
        blur_radius=210,
    )
    draw = ImageDraw.Draw(image, "RGBA")

    eyebrow_font = _load_font(18, bold=True)
    title_font = _load_font(42, bold=True)
    subtitle_font = _load_font(20)
    label_font = _load_font(17, bold=True)
    tick_font = _load_font(18)
    legend_font = _load_font(16, bold=True)
    value_font = _load_font(17, bold=True)

    margin_x = 92
    top_y = 72
    draw.text((margin_x, top_y), "COSMIC VISUAL", font=eyebrow_font, fill="#8591A3")
    title_width = 1040 if len(spec["series"]) > 1 else 1360
    title = _trim_to_width(draw, str(spec["title"]), font=title_font, max_width=title_width)
    draw.text((margin_x, top_y + 38), title, font=title_font, fill="#F7F9FF")
    draw.line((margin_x, top_y + 96, margin_x + 260, top_y + 96), fill=ImageColor.getrgb("#30D158") + (150,), width=3)

    caption = str(spec.get("caption") or "").strip()
    if caption:
        _draw_text_block(
            draw,
            xy=(margin_x, top_y + 116),
            text=caption,
            font=subtitle_font,
            fill="#A8B1C0",
            max_width=980,
            line_gap=4,
            max_lines=2,
        )

    if len(spec["series"]) > 1:
        legend_x = output_width - margin_x
        legend_y = top_y + 42
        for series_index, series in reversed(list(enumerate(spec["series"]))):
            label = _trim_to_width(draw, str(series["label"]), font=legend_font, max_width=220)
            chip_width = _text_size(draw, label, legend_font)[0] + 44
            legend_x -= chip_width
            _draw_simple_chip(
                draw,
                x=legend_x,
                y=legend_y,
                text=label,
                color=_SERIES_PALETTE[series_index % len(_SERIES_PALETTE)],
                font=legend_font,
            )
            legend_x -= 12

    plot_left = 176
    plot_right = output_width - 96
    plot_top = 242
    plot_bottom = 728
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    draw.rounded_rectangle(
        (plot_left - 36, plot_top - 28, plot_right + 24, plot_bottom + 78),
        radius=26,
        fill=(255, 255, 255, 7),
        outline=(255, 255, 255, 18),
        width=1,
    )

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

    min_y, max_y = _nice_range(y_values)

    def y_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_bottom - (ratio * plot_height)

    axis_zero_y = y_to_px(0.0)
    category_count = len(ordered_categories)
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(ordered_categories)
    }

    grid_lines = 5
    for idx in range(grid_lines + 1):
        value = min_y + ((max_y - min_y) * (idx / grid_lines))
        y = y_to_px(value)
        is_zero = abs(value) <= max(abs(max_y - min_y) * 0.02, 1e-9)
        draw.line(
            (plot_left, y, plot_right, y),
            fill=(255, 255, 255, 46 if is_zero else 22),
            width=2 if is_zero else 1,
        )
        tick = _format_tick(value)
        tick_width, tick_height = _text_size(draw, tick, tick_font)
        draw.text((plot_left - tick_width - 24, y - tick_height // 2), tick, font=tick_font, fill="#B3BDCC")

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=(255, 255, 255, 56), width=2)
    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=(255, 255, 255, 34), width=1)

    if spec["chart_type"] == "bar":
        series_count = len(spec["series"])
        group_width = min(category_step * 0.7, 118)
        bar_gap = 8
        bar_width = max(12, (group_width - ((series_count - 1) * bar_gap)) / max(series_count, 1))
        for series_index, series in enumerate(spec["series"]):
            color = _SERIES_PALETTE[series_index % len(_SERIES_PALETTE)]
            for category in ordered_categories:
                point = next((item for item in series["points"] if item["x"] == category), None)
                if point is None:
                    continue
                center = category_centers[category]
                left = int(center - group_width / 2 + (series_index * (bar_width + bar_gap)))
                right = int(left + bar_width)
                top = int(y_to_px(float(point["y"])))
                bottom = int(axis_zero_y)
                if top > bottom:
                    top, bottom = bottom, top
                _draw_bar(draw, bounds=(left, top, right, bottom), color=color)
                if category_count <= 8 and series_count == 1:
                    value = _format_tick(float(point["y"]))
                    value_width, value_height = _text_size(draw, value, value_font)
                    draw.text(
                        (left + ((right - left) // 2) - value_width // 2, max(plot_top, top - value_height - 10)),
                        value,
                        font=value_font,
                        fill="#F1F5FA",
                    )
    else:
        for series_index, series in enumerate(spec["series"]):
            color = _SERIES_PALETTE[series_index % len(_SERIES_PALETTE)]
            rgb = ImageColor.getrgb(color)
            polyline = [
                (category_centers[point["x"]], y_to_px(float(point["y"])))
                for point in series["points"]
                if point["x"] in category_centers
            ]
            if len(polyline) >= 2:
                if series_index == 0:
                    draw.polygon(
                        [(polyline[0][0], plot_bottom)] + polyline + [(polyline[-1][0], plot_bottom)],
                        fill=rgb + (30,),
                    )
                glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
                glow_draw = ImageDraw.Draw(glow, "RGBA")
                glow_draw.line(polyline, fill=rgb + (90,), width=16, joint="curve")
                image = Image.alpha_composite(image, _blur_overlay(glow, 8))
                draw = ImageDraw.Draw(image, "RGBA")
                draw.line(polyline, fill=rgb + (255,), width=5, joint="curve")
            for x, y in polyline:
                draw.ellipse((x - 7, y - 7, x + 7, y + 7), fill=(5, 5, 5, 255), outline=rgb + (255,), width=3)

    label_skip = max(1, int(category_count / 10) + (1 if category_count > 10 and category_count % 10 else 0))
    max_label_width = max(54, int(category_step * 0.86))
    for idx, category in enumerate(ordered_categories):
        if idx % label_skip != 0 and idx != category_count - 1:
            continue
        center = int(plot_left + (idx + 0.5) * category_step)
        label = _trim_to_width(draw, str(category), font=tick_font, max_width=max_label_width)
        label_width, label_height = _text_size(draw, label, tick_font)
        draw.text((center - label_width // 2, plot_bottom + 24), label, font=tick_font, fill="#DDE5F0")

    if spec.get("x_label"):
        x_label = _trim_to_width(draw, str(spec["x_label"]), font=label_font, max_width=520)
        x_width, _ = _text_size(draw, x_label, label_font)
        draw.text((plot_left + plot_width // 2 - x_width // 2, output_height - 70), x_label, font=label_font, fill="#D9E1EC")
    if spec.get("y_label"):
        y_label = _trim_to_width(draw, str(spec["y_label"]), font=label_font, max_width=620)
        draw.text((plot_left, plot_top - 38), y_label, font=label_font, fill="#D9E1EC")

    final_image = Image.alpha_composite(
        Image.new("RGBA", image.size, ImageColor.getrgb("#050505") + (255,)),
        image,
    ).convert("RGB")
    buffer = BytesIO()
    final_image.save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()
