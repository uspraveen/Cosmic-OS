import math
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont

_OUTPUT_WIDTH = 1600
_OUTPUT_HEIGHT = 900

# Plot sits on the response glass. Keep the PNG field transparent so
# `.assistant-inline-image-card.is-chart` shows through instead of a baked slab.
_ACCENT = "#2596be"
_ACCENT_DEEP = "#1a6a88"
_ACCENT_LIGHT = "#7ec8e3"
_SERIES_PALETTE = ("#2596be", "#7ec8e3", "#1a6a88", "#c4a574", "#5d8a9a", "#b8dce8")
_BG = (0, 0, 0, 0)
_INK = "#E8F1F6"
_INK_MUTED = "#8AA3B0"


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
    if abs_value >= 100:
        return f"{value:.0f}"
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
    ellipsis = "…"
    while value and _text_size(draw, f"{value}{ellipsis}", font)[0] > max_width:
        value = value[:-1].rstrip()
    return f"{value}{ellipsis}" if value else ellipsis


def _nice_bounds(values: list[float], *, target_ticks: int = 5) -> tuple[float, float, float]:
    data_min = min(values)
    data_max = max(values)
    lo = 0.0 if data_min >= 0 else data_min
    hi = 0.0 if data_max <= 0 else data_max
    if lo == hi:
        hi = lo + 1.0
    span = hi - lo
    hi += span * 0.08
    if data_min < 0:
        lo -= span * 0.04
    span = hi - lo
    raw_step = span / max(target_ticks, 1)
    if raw_step <= 0:
        raw_step = 1.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = 10 * magnitude
    for multiplier in (1, 2, 2.5, 5, 10):
        if multiplier * magnitude >= raw_step:
            step = multiplier * magnitude
            break
    nice_min = math.floor(lo / step) * step
    nice_max = math.ceil(hi / step) * step
    if nice_min == nice_max:
        nice_max = nice_min + step
    return nice_min, nice_max, step


def _mix_hex(left: str, right: str, ratio: float) -> str:
    clamped = max(0.0, min(1.0, ratio))
    start = ImageColor.getrgb(left)
    end = ImageColor.getrgb(right)
    mixed = tuple(int(start[i] + ((end[i] - start[i]) * clamped)) for i in range(3))
    return "#%02X%02X%02X" % mixed


def _collect_categories(series: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    ordered: list[str] = []
    seen: set[str] = set()
    y_values: list[float] = []
    for item in series:
        for point in item["points"]:
            category = point["x"]
            if category not in seen:
                seen.add(category)
                ordered.append(category)
            y_values.append(float(point["y"]))
    return ordered, y_values


def _should_use_horizontal_bars(spec: dict[str, Any], categories: list[str]) -> bool:
    if spec["chart_type"] != "bar" or not categories:
        return False
    if len(spec["series"]) > 3:
        return False
    lengths = [len(str(category)) for category in categories]
    return max(lengths) >= 10 or (len(categories) <= 6 and (sum(lengths) / len(lengths)) >= 8)


def _series_color(index: int, *, value: float | None = None, value_range: tuple[float, float] | None = None) -> str:
    base = _SERIES_PALETTE[index % len(_SERIES_PALETTE)]
    if index != 0 or value is None or value_range is None:
        return base
    lo, hi = value_range
    span = hi - lo
    if span <= 0:
        return base
    strength = 0.38 + (0.62 * ((value - lo) / span))
    return _mix_hex(_ACCENT_DEEP, _ACCENT_LIGHT, strength)


def _build_canvas(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), _BG)


def _rounded_gradient_bar(
    image: Image.Image,
    *,
    bounds: tuple[int, int, int, int],
    radius: int,
    top_color: str,
    bottom_color: str,
) -> None:
    left, top, right, bottom = (int(value) for value in bounds)
    width = max(1, right - left)
    height = max(1, bottom - top)
    radius = max(0, min(radius, width // 2, height))
    top_rgb = ImageColor.getrgb(top_color)
    bottom_rgb = ImageColor.getrgb(bottom_color)
    gloss_span = max(height * 0.32, 1.0)
    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient, "RGBA")
    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = [int(top_rgb[c] + ((bottom_rgb[c] - top_rgb[c]) * ratio)) for c in range(3)]
        # Bake a soft glossy highlight into the top of the bar so it stays fully
        # opaque (drawing a translucent overlay later would flatten to a dark cap).
        gloss = max(0.0, 1.0 - (y / gloss_span)) * 0.32
        if gloss > 0:
            color = [int(channel + ((255 - channel) * gloss)) for channel in color]
        gradient_draw.line((0, y, width, y), fill=tuple(color) + (255,))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    gradient.putalpha(ImageChops.multiply(gradient.split()[3], mask))
    image.alpha_composite(gradient, (left, top))


def _draw_legend(
    draw: ImageDraw.ImageDraw,
    *,
    series: list[dict[str, Any]],
    x: int,
    y: int,
    max_width: int,
    font: ImageFont.ImageFont,
) -> int:
    chip_gap = 16
    row_gap = 10
    chip_height = max(34, _text_size(draw, "Ag", font)[1] + 16)
    cursor_x = x
    cursor_y = y
    for index, item in enumerate(series):
        label = _trim_to_width(draw, str(item.get("label") or f"Series {index + 1}"), font=font, max_width=280)
        text_width = _text_size(draw, label, font)[0]
        chip_width = text_width + 46
        if cursor_x > x and cursor_x + chip_width > x + max_width:
            cursor_x = x
            cursor_y += chip_height + row_gap
        color = _SERIES_PALETTE[index % len(_SERIES_PALETTE)]
        draw.rounded_rectangle(
            (cursor_x, cursor_y, cursor_x + chip_width, cursor_y + chip_height),
            radius=chip_height // 2,
            fill=(255, 255, 255, 12),
            outline=(255, 255, 255, 26),
            width=1,
        )
        dot_cx = cursor_x + 19
        dot_cy = cursor_y + chip_height // 2
        draw.ellipse((dot_cx - 6, dot_cy - 6, dot_cx + 6, dot_cy + 6), fill=ImageColor.getrgb(color) + (255,))
        text_height = _text_size(draw, label, font)[1]
        draw.text((cursor_x + 33, cursor_y + (chip_height - text_height) // 2 - 1), label, font=font, fill=_INK)
        cursor_x += chip_width + chip_gap
    return cursor_y + chip_height


def _draw_rotated_text(
    image: Image.Image,
    *,
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
    center: tuple[int, int],
) -> None:
    label = str(text or "").strip()
    if not label:
        return
    measure = ImageDraw.Draw(image)
    text_width, text_height = _text_size(measure, label, font)
    tile = Image.new("RGBA", (text_width + 8, text_height + 8), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((4, 4), label, font=font, fill=fill)
    rotated = tile.rotate(90, expand=True)
    paste_x = int(center[0] - rotated.width / 2)
    paste_y = int(center[1] - rotated.height / 2)
    image.alpha_composite(rotated, (paste_x, paste_y))


def render_chart_png(spec: dict[str, Any]) -> bytes:
    """Render a plot-only PNG. Title and caption belong on the glass card, not in the pixels."""
    width = _OUTPUT_WIDTH
    height = _OUTPUT_HEIGHT
    series = spec["series"]
    ordered_categories, y_values = _collect_categories(series)
    if not ordered_categories or not y_values:
        raise ValueError("Chart spec did not contain drawable data.")

    horizontal = _should_use_horizontal_bars(spec, ordered_categories)
    if horizontal:
        height = min(_OUTPUT_HEIGHT, max(560, 160 + (len(ordered_categories) * 108)))

    image = _build_canvas(width, height)
    draw = ImageDraw.Draw(image, "RGBA")
    legend_font = _load_font(16, bold=True)
    axis_title_font = _load_font(17, bold=True)
    tick_font = _load_font(18)
    category_font = _load_font(19)
    value_font = _load_font(17, bold=True)

    multi_series = len(series) > 1
    has_y_label = bool(spec.get("y_label"))
    has_x_label = bool(spec.get("x_label"))
    series_count = len(series)
    category_count = len(ordered_categories)
    min_y, max_y, step = _nice_bounds(y_values)
    value_range = (min(y_values), max(y_values))

    legend_bottom = 36
    if multi_series:
        legend_bottom = _draw_legend(
            draw,
            series=series,
            x=48,
            y=28,
            max_width=width - 96,
            font=legend_font,
        ) + 18

    if horizontal:
        image, draw = _draw_horizontal_bars(
            image,
            spec=spec,
            series=series,
            categories=ordered_categories,
            min_y=min_y,
            max_y=max_y,
            step=step,
            value_range=value_range,
            legend_bottom=legend_bottom,
            fonts={
                "tick": tick_font,
                "category": category_font,
                "value": value_font,
                "axis": axis_title_font,
            },
            has_x_label=has_x_label or has_y_label,
        )
    else:
        image, draw = _draw_vertical_plot(
            image,
            spec=spec,
            series=series,
            categories=ordered_categories,
            min_y=min_y,
            max_y=max_y,
            step=step,
            value_range=value_range,
            legend_bottom=legend_bottom,
            fonts={
                "tick": tick_font,
                "category": category_font,
                "value": value_font,
                "axis": axis_title_font,
            },
            has_x_label=has_x_label,
            has_y_label=has_y_label,
            multi_series=multi_series,
            series_count=series_count,
            category_count=category_count,
        )

    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()


def _draw_vertical_plot(
    image: Image.Image,
    *,
    spec: dict[str, Any],
    series: list[dict[str, Any]],
    categories: list[str],
    min_y: float,
    max_y: float,
    step: float,
    value_range: tuple[float, float],
    legend_bottom: int,
    fonts: dict[str, ImageFont.ImageFont],
    has_x_label: bool,
    has_y_label: bool,
    multi_series: bool,
    series_count: int,
    category_count: int,
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    tick_font = fonts["tick"]
    category_font = fonts["category"]
    value_font = fonts["value"]
    axis_title_font = fonts["axis"]

    plot_left = 156 if has_y_label else 108
    plot_right = width - 56
    plot_top = max(legend_bottom + 8, 52)
    plot_bottom = height - (108 if has_x_label else 78)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    def y_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_bottom - (ratio * plot_height)

    axis_zero_y = max(plot_top, min(plot_bottom, y_to_px(0.0)))
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(categories)
    }

    tick_value = min_y
    while tick_value <= max_y + (step * 0.001):
        y = y_to_px(tick_value)
        is_zero = abs(tick_value) <= step * 0.001
        draw.line(
            (plot_left, y, plot_right, y),
            fill=(255, 255, 255, 40 if is_zero else 16),
            width=1,
        )
        tick_text = _format_tick(tick_value)
        tick_width, tick_height = _text_size(draw, tick_text, tick_font)
        draw.text((plot_left - tick_width - 16, y - tick_height // 2), tick_text, font=tick_font, fill=_INK_MUTED)
        tick_value += step

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=ImageColor.getrgb(_ACCENT) + (70,), width=2)

    show_bar_values = series_count * category_count <= 22
    if spec["chart_type"] == "bar":
        inner_gap = 10 if series_count > 1 else 0
        group_width = min(category_step * 0.58, (series_count * 92) + ((series_count - 1) * inner_gap))
        bar_width = max(18.0, (group_width - ((series_count - 1) * inner_gap)) / max(series_count, 1))
        group_width = (bar_width * series_count) + (inner_gap * (series_count - 1))
        for series_index, item in enumerate(series):
            for category in categories:
                point = next((entry for entry in item["points"] if entry["x"] == category), None)
                if point is None:
                    continue
                value = float(point["y"])
                color = _series_color(
                    series_index,
                    value=value if not multi_series else None,
                    value_range=value_range if not multi_series else None,
                )
                top_color = _mix_hex(color, "#FFFFFF", 0.22)
                center = category_centers[category]
                left = center - (group_width / 2) + (series_index * (bar_width + inner_gap))
                right = left + bar_width
                top = y_to_px(value)
                bottom = axis_zero_y
                if top > bottom:
                    top, bottom = bottom, top
                if bottom - top < 4:
                    top = bottom - 4
                radius = max(6, int(min(18, bar_width / 2.2)))
                _rounded_gradient_bar(
                    image,
                    bounds=(left, top, right, bottom),
                    radius=radius,
                    top_color=top_color,
                    bottom_color=color,
                )
                draw = ImageDraw.Draw(image, "RGBA")
                if show_bar_values:
                    value_text = _format_tick(value)
                    value_width, value_height = _text_size(draw, value_text, value_font)
                    label_x = int(left + (bar_width / 2) - (value_width / 2))
                    label_y = int(top - value_height - 10)
                    if label_y < plot_top:
                        label_y = int(top + 8)
                    draw.text((label_x, label_y), value_text, font=value_font, fill=_INK)
    else:
        for series_index, item in enumerate(series):
            color = _SERIES_PALETTE[series_index % len(_SERIES_PALETTE)]
            rgb = ImageColor.getrgb(color)
            polyline = [
                (category_centers[point["x"]], y_to_px(float(point["y"])))
                for point in item["points"]
                if point["x"] in category_centers
            ]
            if len(polyline) >= 2:
                if series_index == 0:
                    draw.polygon(
                        [(polyline[0][0], plot_bottom)] + polyline + [(polyline[-1][0], plot_bottom)],
                        fill=rgb + (28,),
                    )
                glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
                ImageDraw.Draw(glow, "RGBA").line(polyline, fill=rgb + (70,), width=12, joint="curve")
                image = Image.alpha_composite(image, _blur_overlay(glow, 6))
                draw = ImageDraw.Draw(image, "RGBA")
                draw.line(polyline, fill=rgb + (255,), width=4, joint="curve")
            for x, y in polyline:
                draw.ellipse(
                    (x - 6, y - 6, x + 6, y + 6),
                    fill=rgb + (255,),
                    outline=_INK,
                    width=2,
                )

    rotate_labels = False
    max_natural = 0
    for category in categories:
        max_natural = max(max_natural, _text_size(draw, str(category), category_font)[0])
    if max_natural > category_step * 0.9 and category_count <= 10:
        rotate_labels = True

    for idx, category in enumerate(categories):
        center = int(plot_left + (idx + 0.5) * category_step)
        if rotate_labels:
            label = _trim_to_width(draw, str(category), font=category_font, max_width=168)
            tile_w, tile_h = _text_size(draw, label, category_font)
            tile = Image.new("RGBA", (tile_w + 8, tile_h + 8), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((4, 4), label, font=category_font, fill=_INK)
            rotated = tile.rotate(32, expand=True, resample=Image.BICUBIC)
            paste_x = center - 8
            paste_y = plot_bottom + 14
            image.alpha_composite(rotated, (paste_x, paste_y))
            draw = ImageDraw.Draw(image, "RGBA")
        else:
            label = _trim_to_width(draw, str(category), font=category_font, max_width=max(64, int(category_step * 0.94)))
            label_width = _text_size(draw, label, category_font)[0]
            draw.text((center - label_width // 2, plot_bottom + 18), label, font=category_font, fill=_INK)

    if has_x_label:
        x_label = _trim_to_width(draw, str(spec["x_label"]), font=axis_title_font, max_width=plot_width)
        x_label_width = _text_size(draw, x_label, axis_title_font)[0]
        draw.text(
            (plot_left + (plot_width // 2) - (x_label_width // 2), height - 36),
            x_label,
            font=axis_title_font,
            fill=_INK_MUTED,
        )
    if has_y_label:
        _draw_rotated_text(
            image,
            text=_trim_to_width(draw, str(spec["y_label"]), font=axis_title_font, max_width=plot_height),
            font=axis_title_font,
            fill=_INK_MUTED,
            center=(36, (plot_top + plot_bottom) // 2),
        )
    return image, draw


def _draw_horizontal_bars(
    image: Image.Image,
    *,
    spec: dict[str, Any],
    series: list[dict[str, Any]],
    categories: list[str],
    min_y: float,
    max_y: float,
    step: float,
    value_range: tuple[float, float],
    legend_bottom: int,
    fonts: dict[str, ImageFont.ImageFont],
    has_x_label: bool,
) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    width, height = image.size
    draw = ImageDraw.Draw(image, "RGBA")
    tick_font = fonts["tick"]
    category_font = fonts["category"]
    value_font = fonts["value"]
    axis_title_font = fonts["axis"]
    series_count = len(series)
    multi_series = series_count > 1

    label_widths = [_text_size(draw, str(category), category_font)[0] for category in categories]
    label_column = min(420, max(label_widths) + 8)
    plot_left = 48 + label_column + 20
    plot_right = width - 92
    plot_top = max(legend_bottom + 12, 48)
    plot_bottom = height - (72 if has_x_label else 52)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    row_step = plot_height / max(len(categories), 1)

    def x_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_left + (ratio * plot_width)

    axis_zero_x = max(plot_left, min(plot_right, x_to_px(0.0)))

    tick_value = min_y
    while tick_value <= max_y + (step * 0.001):
        x = x_to_px(tick_value)
        is_zero = abs(tick_value) <= step * 0.001
        draw.line(
            (x, plot_top, x, plot_bottom),
            fill=(255, 255, 255, 36 if is_zero else 14),
            width=1,
        )
        tick_text = _format_tick(tick_value)
        tick_width, tick_height = _text_size(draw, tick_text, tick_font)
        draw.text((x - tick_width // 2, plot_bottom + 12), tick_text, font=tick_font, fill=_INK_MUTED)
        tick_value += step

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=ImageColor.getrgb(_ACCENT) + (70,), width=2)

    inner_gap = 8 if multi_series else 0
    group_height = min(row_step * 0.58, (series_count * 36) + ((series_count - 1) * inner_gap))
    bar_height = max(16.0, (group_height - ((series_count - 1) * inner_gap)) / max(series_count, 1))
    group_height = (bar_height * series_count) + (inner_gap * (series_count - 1))

    for idx, category in enumerate(categories):
        row_center = plot_top + (idx + 0.5) * row_step
        label = _trim_to_width(draw, str(category), font=category_font, max_width=label_column)
        _, label_height = _text_size(draw, label, category_font)
        draw.text(
            (48, int(row_center - label_height / 2)),
            label,
            font=category_font,
            fill=_INK,
        )
        for series_index, item in enumerate(series):
            point = next((entry for entry in item["points"] if entry["x"] == category), None)
            if point is None:
                continue
            value = float(point["y"])
            color = _series_color(
                series_index,
                value=value if not multi_series else None,
                value_range=value_range if not multi_series else None,
            )
            top_color = _mix_hex(color, "#FFFFFF", 0.22)
            top = row_center - (group_height / 2) + (series_index * (bar_height + inner_gap))
            bottom = top + bar_height
            left = axis_zero_x
            right = x_to_px(value)
            if right < left:
                left, right = right, left
            if right - left < 4:
                right = left + 4
            radius = max(6, int(min(16, bar_height / 2.1)))
            _rounded_gradient_bar(
                image,
                bounds=(left, top, right, bottom),
                radius=radius,
                top_color=top_color,
                bottom_color=color,
            )
            draw = ImageDraw.Draw(image, "RGBA")
            value_text = _format_tick(value)
            value_width, value_height = _text_size(draw, value_text, value_font)
            label_x = int(right + 12)
            if label_x + value_width > width - 24:
                label_x = int(right - value_width - 12)
            draw.text(
                (label_x, int(top + (bar_height - value_height) / 2)),
                value_text,
                font=value_font,
                fill=_INK,
            )

    axis_caption = str(spec.get("x_label") or spec.get("y_label") or "").strip()
    if has_x_label and axis_caption:
        caption = _trim_to_width(draw, axis_caption, font=axis_title_font, max_width=plot_width)
        caption_width = _text_size(draw, caption, axis_title_font)[0]
        draw.text(
            (plot_left + (plot_width // 2) - (caption_width // 2), height - 32),
            caption,
            font=axis_title_font,
            fill=_INK_MUTED,
        )
    return image, draw
