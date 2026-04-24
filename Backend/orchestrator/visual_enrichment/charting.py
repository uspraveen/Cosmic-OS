from __future__ import annotations

from io import BytesIO
from typing import Any

from PIL import Image, ImageColor, ImageDraw, ImageFont


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


def render_chart_png(spec: dict[str, Any]) -> bytes:
    width = 1200
    height = 720
    margin_left = 110
    margin_right = 52
    margin_top = 92
    margin_bottom = 110
    plot_left = margin_left
    plot_right = width - margin_right
    plot_top = margin_top
    plot_bottom = height - margin_bottom
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    image = Image.new("RGBA", (width, height), ImageColor.getrgb("#0c1220"))
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default()
    label_font = ImageFont.load_default()
    tick_font = ImageFont.load_default()

    draw.rounded_rectangle(
        (20, 20, width - 20, height - 20),
        radius=28,
        outline="#2a3a54",
        width=2,
        fill="#0f172a",
    )
    draw.text((plot_left, 42), spec["title"], font=title_font, fill="#f8fbff")

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
    value_padding = (max_y - min_y) * 0.1
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
        draw.line((plot_left, y, plot_right, y), fill="#22314a", width=1)
        draw.text((34, y - 7), f"{value:.2f}".rstrip("0").rstrip("."), font=tick_font, fill="#9fb0c7")

    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill="#7287a7", width=2)
    draw.line((plot_left, axis_zero_y, plot_right, axis_zero_y), fill="#7287a7", width=2)

    palette = ["#79c9ff", "#ffd67a", "#9ef0b7", "#ff9bb4"]
    category_count = len(ordered_categories)
    category_step = plot_width / max(category_count, 1)
    category_centers = {
        category: plot_left + (idx + 0.5) * category_step
        for idx, category in enumerate(ordered_categories)
    }

    if spec["chart_type"] == "bar":
        series_count = len(spec["series"])
        group_width = category_step * 0.7
        bar_width = group_width / max(series_count, 1)
        for series_index, series in enumerate(spec["series"]):
            color = palette[series_index % len(palette)]
            for category in ordered_categories:
                point = next((item for item in series["points"] if item["x"] == category), None)
                if point is None:
                    continue
                center = category_centers[category]
                left = center - (group_width / 2) + (series_index * bar_width) + 4
                right = left + max(12, bar_width - 8)
                top = y_to_px(float(point["y"]))
                bottom = axis_zero_y
                if top > bottom:
                    top, bottom = bottom, top
                draw.rounded_rectangle(
                    (left, top, right, bottom),
                    radius=8,
                    fill=color,
                    outline=None,
                )
    else:
        for series_index, series in enumerate(spec["series"]):
            color = palette[series_index % len(palette)]
            polyline: list[tuple[float, float]] = []
            for point in series["points"]:
                polyline.append((category_centers[point["x"]], y_to_px(float(point["y"]))))
            if len(polyline) >= 2:
                draw.line(polyline, fill=color, width=4, joint="curve")
            for x, y in polyline:
                draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=color, outline="#0f172a", width=2)

    for idx, category in enumerate(ordered_categories):
        center = plot_left + (idx + 0.5) * category_step
        text = category
        draw.text((center - (len(text) * 3), plot_bottom + 18), text, font=tick_font, fill="#c8d4e6")

    if spec.get("x_label"):
        draw.text((plot_left + (plot_width / 2) - 40, height - 54), str(spec["x_label"]), font=label_font, fill="#d9e4f2")
    if spec.get("y_label"):
        draw.text((36, plot_top - 28), str(spec["y_label"]), font=label_font, fill="#d9e4f2")

    legend_x = plot_right - 210
    legend_y = 38
    for series_index, series in enumerate(spec["series"]):
        color = palette[series_index % len(palette)]
        y = legend_y + (series_index * 22)
        draw.rounded_rectangle((legend_x, y + 2, legend_x + 14, y + 16), radius=4, fill=color)
        draw.text((legend_x + 24, y), str(series["label"]), font=label_font, fill="#d9e4f2")

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
