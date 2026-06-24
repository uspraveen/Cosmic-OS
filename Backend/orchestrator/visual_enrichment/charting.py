import math
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont

_OUTPUT_WIDTH = 1600
_OUTPUT_HEIGHT = 900

# Shared COSMIC chart theme. These values mirror the liquid-glass card the chart
# is rendered inside (see `.assistant-inline-image-card` in spotlight.css): a dark
# vertical gradient with a blue glow top-left and a teal glow bottom-right, plus the
# Cosmic blue/teal accent duo. Keeping them in sync makes the PNG melt into its card.
_SERIES_PALETTE = ("#0A84FF", "#4DEAB2", "#FF9F0A", "#BF5AF2", "#FF566A", "#64D2FF")
_BG_TOP = "#0E111C"
_BG_BOTTOM = "#07090F"
_INK = "#F5F8FF"
_INK_MUTED = "#A8B2C4"
_INK_FAINT = "#7E8BA0"
_ACCENT_BLUE = "#5BA3FF"
_ACCENT_TEAL = "#4DEAB2"


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


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = str(text or "").strip().split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_size(draw, candidate, font)[0] <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if words and len(lines) == max_lines:
        lines[-1] = _trim_to_width(draw, lines[-1], font=font, max_width=max_width)
    return lines


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
    lines = _wrap_lines(draw, text, font=font, max_width=max_width, max_lines=max_lines)
    y = xy[1]
    for line in lines:
        draw.text((xy[0], y), line, font=font, fill=fill)
        y += _text_size(draw, line, font)[1] + line_gap
    return y


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


def _build_canvas() -> Image.Image:
    image = Image.new("RGBA", (_OUTPUT_WIDTH, _OUTPUT_HEIGHT), ImageColor.getrgb(_BG_BOTTOM) + (255,))
    image = _add_vertical_gradient(image, top_color=_BG_TOP, bottom_color=_BG_BOTTOM, alpha=210)
    # Blue glow top-left + teal glow bottom-right, matching the card's radial accents.
    image = _add_glow(
        image,
        bounds=(-320, -300, 700, 560),
        color=_ACCENT_BLUE,
        alpha=42,
        blur_radius=220,
    )
    image = _add_glow(
        image,
        bounds=(980, 520, 1900, 1240),
        color=_ACCENT_TEAL,
        alpha=30,
        blur_radius=240,
    )
    return image


def _horizontal_gradient_pill(
    image: Image.Image,
    *,
    bounds: tuple[int, int, int, int],
    radius: int,
    left_color: str,
    right_color: str,
) -> None:
    left, top, right, bottom = (int(value) for value in bounds)
    width = max(1, right - left)
    height = max(1, bottom - top)
    radius = max(0, min(radius, height // 2))
    left_rgb = ImageColor.getrgb(left_color)
    right_rgb = ImageColor.getrgb(right_color)
    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient, "RGBA")
    for x in range(width):
        ratio = x / max(width - 1, 1)
        color = tuple(int(left_rgb[c] + ((right_rgb[c] - left_rgb[c]) * ratio)) for c in range(3))
        gradient_draw.line((x, 0, x, height), fill=color + (255,))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    gradient.putalpha(ImageChops.multiply(gradient.split()[3], mask))
    image.alpha_composite(gradient, (left, top))


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
        draw.text((cursor_x + 33, cursor_y + (chip_height - text_height) // 2 - 1), label, font=font, fill="#E8EEF8")
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
    width = _OUTPUT_WIDTH
    height = _OUTPUT_HEIGHT
    image = _build_canvas()
    draw = ImageDraw.Draw(image, "RGBA")

    eyebrow_font = _load_font(17, bold=True)
    title_font = _load_font(40, bold=True)
    caption_font = _load_font(20)
    legend_font = _load_font(16, bold=True)
    axis_title_font = _load_font(17, bold=True)
    tick_font = _load_font(18)
    category_font = _load_font(18)
    value_font = _load_font(16, bold=True)

    series = spec["series"]
    multi_series = len(series) > 1
    margin_x = 96
    header_width = width - (2 * margin_x)

    # --- Header: eyebrow, title, accent rule, caption ---
    eyebrow_y = 58
    draw.text((margin_x, eyebrow_y), "COSMIC VISUAL", font=eyebrow_font, fill=_INK_FAINT)

    title_y = eyebrow_y + 28
    title = _trim_to_width(draw, str(spec["title"]), font=title_font, max_width=header_width)
    draw.text((margin_x, title_y), title, font=title_font, fill=_INK)
    title_bottom = draw.textbbox((margin_x, title_y), title or "Ag", font=title_font)[3]

    rule_y = title_bottom + 14
    _horizontal_gradient_pill(
        image,
        bounds=(margin_x, rule_y, margin_x + 232, rule_y + 5),
        radius=3,
        left_color=_ACCENT_BLUE,
        right_color=_ACCENT_TEAL,
    )
    draw = ImageDraw.Draw(image, "RGBA")

    cursor_y = rule_y + 20
    caption = str(spec.get("caption") or "").strip()
    if caption:
        cursor_y = _draw_text_block(
            draw,
            xy=(margin_x, cursor_y),
            text=caption,
            font=caption_font,
            fill=_INK_MUTED,
            max_width=header_width,
            line_gap=6,
            max_lines=2,
        )
        cursor_y += 6

    # --- Legend (its own row beneath the header, never over the title) ---
    if multi_series:
        cursor_y = _draw_legend(
            draw,
            series=series,
            x=margin_x,
            y=cursor_y + 4,
            max_width=header_width,
            font=legend_font,
        )

    # --- Plot frame (dynamic top so nothing overlaps the header) ---
    has_y_label = bool(spec.get("y_label"))
    has_x_label = bool(spec.get("x_label"))
    plot_left = 168 if has_y_label else 132
    plot_right = width - 80
    plot_top = max(int(cursor_y) + 30, 268)
    plot_bottom = height - (132 if has_x_label else 96)
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    draw.rounded_rectangle(
        (plot_left - 34, plot_top - 26, plot_right + 22, plot_bottom + 24),
        radius=24,
        fill=(255, 255, 255, 6),
        outline=(255, 255, 255, 16),
        width=1,
    )

    ordered_categories: list[str] = []
    seen_categories: set[str] = set()
    y_values: list[float] = []
    for item in series:
        for point in item["points"]:
            category = point["x"]
            if category not in seen_categories:
                seen_categories.add(category)
                ordered_categories.append(category)
            y_values.append(float(point["y"]))
    if not ordered_categories or not y_values:
        raise ValueError("Chart spec did not contain drawable data.")

    min_y, max_y, step = _nice_bounds(y_values)

    def y_to_px(value: float) -> float:
        ratio = (value - min_y) / (max_y - min_y)
        return plot_bottom - (ratio * plot_height)

    axis_zero_y = max(plot_top, min(plot_bottom, y_to_px(0.0)))
    category_count = len(ordered_categories)
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(ordered_categories)
    }

    # --- Horizontal gridlines + y ticks ---
    tick_value = min_y
    while tick_value <= max_y + (step * 0.001):
        y = y_to_px(tick_value)
        is_zero = abs(tick_value) <= step * 0.001
        draw.line(
            (plot_left, y, plot_right, y),
            fill=(255, 255, 255, 48 if is_zero else 18),
            width=2 if is_zero else 1,
        )
        tick_text = _format_tick(tick_value)
        tick_width, tick_height = _text_size(draw, tick_text, tick_font)
        draw.text((plot_left - tick_width - 22, y - tick_height // 2), tick_text, font=tick_font, fill=_INK_MUTED)
        tick_value += step

    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill=(255, 255, 255, 34), width=1)
    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill=(255, 255, 255, 60), width=2)

    series_count = len(series)
    show_bar_values = series_count * category_count <= 22

    if spec["chart_type"] == "bar":
        inner_gap = 8 if series_count > 1 else 0
        group_width = min(category_step * 0.72, (series_count * 84) + ((series_count - 1) * inner_gap))
        bar_width = max(12.0, (group_width - ((series_count - 1) * inner_gap)) / max(series_count, 1))
        group_width = (bar_width * series_count) + (inner_gap * (series_count - 1))
        for series_index, item in enumerate(series):
            color = _SERIES_PALETTE[series_index % len(_SERIES_PALETTE)]
            base_rgb = ImageColor.getrgb(color)
            top_rgb = tuple(min(255, channel + 60) for channel in base_rgb)
            top_color = "#%02X%02X%02X" % top_rgb
            for category in ordered_categories:
                point = next((entry for entry in item["points"] if entry["x"] == category), None)
                if point is None:
                    continue
                center = category_centers[category]
                left = center - (group_width / 2) + (series_index * (bar_width + inner_gap))
                right = left + bar_width
                value = float(point["y"])
                top = y_to_px(value)
                bottom = axis_zero_y
                if top > bottom:
                    top, bottom = bottom, top
                if bottom - top < 3:
                    top = bottom - 3
                radius = max(4, int(min(14, bar_width / 2.4)))
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
                    label_y = int(top - value_height - 8)
                    if label_y < plot_top:
                        label_y = int(top + 8)
                    draw.text((label_x, label_y), value_text, font=value_font, fill="#EEF3FB")
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
                        fill=rgb + (30,),
                    )
                glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
                ImageDraw.Draw(glow, "RGBA").line(polyline, fill=rgb + (90,), width=16, joint="curve")
                image = Image.alpha_composite(image, _blur_overlay(glow, 8))
                draw = ImageDraw.Draw(image, "RGBA")
                draw.line(polyline, fill=rgb + (255,), width=5, joint="curve")
            for x, y in polyline:
                draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=ImageColor.getrgb(_BG_BOTTOM) + (255,), outline=rgb + (255,), width=3)

    # --- X category labels ---
    label_skip = max(1, math.ceil(category_count / 12))
    max_label_width = max(56, int(category_step * 0.92))
    for idx, category in enumerate(ordered_categories):
        if idx % label_skip != 0 and idx != category_count - 1:
            continue
        center = int(plot_left + (idx + 0.5) * category_step)
        label = _trim_to_width(draw, str(category), font=category_font, max_width=max_label_width)
        label_width = _text_size(draw, label, category_font)[0]
        draw.text((center - label_width // 2, plot_bottom + 20), label, font=category_font, fill="#DDE5F0")

    # --- Axis titles ---
    if has_x_label:
        x_label = _trim_to_width(draw, str(spec["x_label"]), font=axis_title_font, max_width=plot_width)
        x_label_width = _text_size(draw, x_label, axis_title_font)[0]
        draw.text(
            (plot_left + (plot_width // 2) - (x_label_width // 2), height - 64),
            x_label,
            font=axis_title_font,
            fill="#C8D2E0",
        )
    if has_y_label:
        _draw_rotated_text(
            image,
            text=_trim_to_width(draw, str(spec["y_label"]), font=axis_title_font, max_width=plot_height),
            font=axis_title_font,
            fill="#C8D2E0",
            center=(plot_left - 118, (plot_top + plot_bottom) // 2),
        )

    final_image = Image.alpha_composite(
        Image.new("RGBA", image.size, ImageColor.getrgb(_BG_BOTTOM) + (255,)),
        image,
    ).convert("RGB")
    buffer = BytesIO()
    final_image.save(buffer, format="PNG", compress_level=3)
    return buffer.getvalue()
