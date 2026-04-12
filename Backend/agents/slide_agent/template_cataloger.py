"""Template cataloger — cosmic-slides-2.

Produces a pure selection catalog for each template PPTX.
The catalog's only job is to help the slide builder pick the right
template slide. All zone extraction and filling happens at build time.

Output per template  (catalogs/<template_stem>/)
────────────────────────────────────────────────
  catalog.json   ← slide-level visual descriptions + archetypes
  collage.png    ← numbered grid of all slide thumbnails
  thumbnails/    ← individual slide PNGs

Each slide entry in catalog.json
──────────────────────────────────
  slide_number       int    1-indexed
  thumbnail_path     str    path to individual PNG
  visual_description str    precise LLM-generated visual layout description
  layout_archetype   str    one of the canonical archetype labels

Re-running always replaces the existing catalog.json.

Usage
─────
  python template_cataloger.py templates/Startup_pitch_deck.pptx
  python template_cataloger.py templates/Startup_pitch_deck.pptx --force
  python template_cataloger.py templates/Startup_pitch_deck.pptx --verbose
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

# ── Config ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

MODEL_BASE_URL: str      = os.getenv("MODEL_BASE_URL", "https://api.fireworks.ai/inference/v1").rstrip("/")
MODEL_API_KEY: str       = os.getenv("MODEL_API_KEY", "")
VISION_MODEL: str        = os.getenv("VISION_MODEL_NAME", "accounts/fireworks/models/qwen3p6-plus")
LIBREOFFICE_PATH: str    = os.getenv("LIBREOFFICE_PATH", "soffice")
PDFTOPPM_PATH: str       = os.getenv("PDFTOPPM_PATH", "pdftoppm")
CATALOGS_DIR: Path       = _HERE / os.getenv("CATALOGS_DIR", "catalogs")
CATALOG_PARALLELISM: int = int(os.getenv("CATALOG_PARALLELISM", "5"))

logger = logging.getLogger(__name__)

# ── Layout archetypes ──────────────────────────────────────────────────────────

LAYOUT_ARCHETYPES = [
    "cover",
    "section-break",
    "title-body",
    "two-column",
    "three-column",
    "four-column",
    "full-bleed-image",
    "big-stat",
    "quote",
    "timeline",
    "comparison",
    "grid",
    "people-showcase",
    "chart-focus",
    "closing",
    "other",
]

# ── Subprocess helper ──────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    kw: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kw)

# ── Rendering ──────────────────────────────────────────────────────────────────

def render_template_to_pngs(pptx_path: Path, output_dir: Path, dpi: int = 150) -> list[Path]:
    """Render all slides to individual PNGs via LibreOffice + pdftoppm."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = output_dir.parent / f".render_tmp_{pptx_path.stem}"
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("render: converting %s to PDF …", pptx_path.name)
        result = _run([
            LIBREOFFICE_PATH, "--headless",
            "--convert-to", "pdf:impress_pdf_Export",
            "--outdir", str(tmp_path),
            str(pptx_path),
        ])
        if result.returncode != 0:
            raise RuntimeError(f"LibreOffice failed (exit {result.returncode}):\n{result.stderr}")

        pdf_files = list(tmp_path.glob("*.pdf"))
        if not pdf_files:
            raise RuntimeError("LibreOffice produced no PDF output.")

        logger.info("render: converting PDF to PNGs at %d dpi …", dpi)
        result = _run([
            PDFTOPPM_PATH, "-png", "-r", str(dpi),
            str(pdf_files[0]),
            str(output_dir / "slide"),
        ])
        if result.returncode != 0:
            raise RuntimeError(f"pdftoppm failed (exit {result.returncode}):\n{result.stderr}")
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)

    pngs = sorted(output_dir.glob("slide-*.png"))
    if not pngs:
        raise RuntimeError("pdftoppm produced no PNG output.")
    logger.info("render: %d slide PNGs → %s", len(pngs), output_dir)
    return pngs


# ── Collage ────────────────────────────────────────────────────────────────────

def create_numbered_collage(
    slide_pngs: list[Path],
    output_path: Path,
    *,
    columns: int = 4,
    thumb_w: int = 480,
    thumb_h: int = 270,
    padding: int = 10,
) -> Path:
    """Grid collage with amber slide-number badges (top-left) and label bars (bottom)."""
    from PIL import Image, ImageDraw, ImageFont

    n     = len(slide_pngs)
    rows  = (n + columns - 1) // columns
    bar_h = 36
    cw    = thumb_w + padding * 2
    ch    = thumb_h + bar_h + padding * 2

    canvas = Image.new("RGB", (cw * columns, ch * rows), (30, 30, 30))
    draw   = ImageDraw.Draw(canvas)

    def _font(size: int):
        for name in ("arialbd.ttf", "arial.ttf", "C:/Windows/Fonts/arialbd.ttf",
                     "C:/Windows/Fonts/arial.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    f_label = _font(20)
    f_badge = _font(22)

    for idx, png in enumerate(slide_pngs):
        row, col = divmod(idx, columns)
        x0 = col * cw + padding
        y0 = row * ch + padding

        try:
            img = Image.open(png).convert("RGB").resize((thumb_w, thumb_h), Image.LANCZOS)
            canvas.paste(img, (x0, y0))
        except Exception:
            draw.rectangle([x0, y0, x0 + thumb_w, y0 + thumb_h], fill=(60, 60, 60))

        # Bottom label bar
        bar_y = y0 + thumb_h
        draw.rectangle([x0, bar_y, x0 + thumb_w, bar_y + bar_h], fill=(20, 20, 20))
        draw.text((x0 + thumb_w // 2, bar_y + bar_h // 2), f"SLIDE {idx + 1}",
                  fill=(255, 255, 255), font=f_label, anchor="mm")

        # Amber corner badge
        bs = 40
        draw.rectangle([x0, y0, x0 + bs, y0 + bs], fill=(255, 180, 0))
        draw.text((x0 + bs // 2, y0 + bs // 2), str(idx + 1),
                  fill=(0, 0, 0), font=f_badge, anchor="mm")

        draw.rectangle([x0 - 1, y0 - 1, x0 + thumb_w, y0 + thumb_h + bar_h],
                       outline=(80, 80, 80), width=1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(output_path), "PNG", optimize=True)
    logger.info("collage: saved %s (%d slides)", output_path, n)
    return output_path


# ── Vision LLM ────────────────────────────────────────────────────────────────

_VISION_SYSTEM = """\
You are a presentation slide layout analyst.
You look at slide images and describe their visual structure precisely.
This description is used by an AI to match content to the right layout.
Focus on structure only — not on the topic or words shown.
Return ONLY valid JSON. No markdown, no explanation, no extra text.
"""

_VISION_USER = """\
Analyze the visual layout of this presentation slide (Slide {n} of {total}).

Return ONLY this JSON:
{{
  "visual_description": "2-3 sentences describing the slide layout precisely. \
Include: background color/style, number and arrangement of content regions, \
title area size and position, body/text areas, image or icon placeholders, \
decorative elements, approximate proportions (e.g. left 60%% / right 40%%, \
three equal columns, full-bleed background).",
  "layout_archetype": "one of: {archetypes}"
}}

Example of a good visual_description:
"Dark navy full-bleed background with a thin gold accent line beneath the header. \
Large bold white title spanning the full width in the upper third. Three equal columns \
below, each with a circular icon area at top, a short bold label, and 3-4 lines of \
body text. Thin white vertical dividers separate the columns."
"""


def _vision_describe_slide(
    png_path: Path,
    slide_number: int,
    total: int,
    client: httpx.Client,
) -> dict:
    """Call vision LLM on one slide thumbnail. Returns visual_description + layout_archetype."""
    image_b64 = base64.b64encode(png_path.read_bytes()).decode("utf-8")
    user_text = _VISION_USER.format(
        n=slide_number,
        total=total,
        archetypes=" | ".join(LAYOUT_ARCHETYPES),
    )

    messages = [
        {"role": "system", "content": _VISION_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": user_text},
            ],
        },
    ]

    payload = {
        "model":       VISION_MODEL,
        "messages":    messages,
        "temperature": 0.2,
        "max_tokens":  4096,
        "stream":      True,
    }
    headers = {
        "Authorization": f"Bearer {MODEL_API_KEY}",
        "Content-Type":  "application/json",
    }
    url = f"{MODEL_BASE_URL}/chat/completions"

    for attempt in range(3):
        try:
            raw = ""
            with client.stream("POST", url, json=payload, headers=headers, timeout=90) as resp:
                if resp.status_code >= 400:
                    raise ValueError(f"API {resp.status_code}: {resp.read().decode()[:300]}")
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
                        raw += piece
                    except Exception:
                        continue

            raw = raw.strip()
            if not raw:
                raise ValueError("Empty response")

            if raw.startswith("```"):
                raw = "\n".join(l for l in raw.splitlines()
                                if not l.strip().startswith("```")).strip()
            if "{" in raw and not raw.startswith("{"):
                raw = raw[raw.index("{"):]
            if "}" in raw and not raw.endswith("}"):
                raw = raw[:raw.rindex("}") + 1]

            result = json.loads(raw)
            if result.get("layout_archetype") not in LAYOUT_ARCHETYPES:
                result["layout_archetype"] = "other"
            return result

        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("vision slide %d attempt %d: %s", slide_number, attempt + 1, exc)
            if attempt < 2:
                time.sleep(1)

    logger.error("vision slide %d: all attempts failed", slide_number)
    return {"visual_description": "Vision analysis failed.", "layout_archetype": "other"}


# ── Catalog I/O ───────────────────────────────────────────────────────────────

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _catalog_path(template_path: Path) -> Path:
    return CATALOGS_DIR / template_path.stem / "catalog.json"


def load_catalog(template_path: Path) -> dict | None:
    """Return cached catalog if it exists and the template hasn't changed."""
    cat_path = _catalog_path(template_path)
    if not cat_path.exists():
        return None
    try:
        data = json.loads(cat_path.read_text(encoding="utf-8"))
        if data.get("template_hash") == _file_hash(template_path):
            return data
        logger.info("catalog: template changed — will re-catalog")
    except Exception as exc:
        logger.warning("catalog: load failed: %s", exc)
    return None


def _save_catalog(catalog: dict, template_path: Path) -> None:
    cat_path = _catalog_path(template_path)
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    cat_path.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("catalog: saved → %s", cat_path)


# ── Main entry point ───────────────────────────────────────────────────────────

def catalog_template(template_path: Path, *, force: bool = False) -> dict:
    """Catalog one template PPTX.

    1. Render all slides to PNG
    2. Build numbered collage
    3. Call vision LLM on each slide (parallel)
    4. Save catalog.json

    Returns the catalog dict.
    """
    template_path = template_path.resolve()
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    if not MODEL_API_KEY:
        raise ValueError("MODEL_API_KEY is not set in .env")

    if not force:
        cached = load_catalog(template_path)
        if cached:
            logger.info("catalog: using cached catalog for '%s'", template_path.stem)
            return cached

    cat_dir      = CATALOGS_DIR / template_path.stem
    thumbs_dir   = cat_dir / "thumbnails"
    collage_path = cat_dir / "collage.png"

    # 1. Render
    logger.info("=== Cataloging '%s' ===", template_path.name)
    slide_pngs = render_template_to_pngs(template_path, thumbs_dir)
    total      = len(slide_pngs)

    # 2. Collage
    create_numbered_collage(slide_pngs, collage_path)

    # 3. Vision LLM — one call per slide, parallelized
    def _process(args: tuple[int, Path]) -> tuple[int, dict]:
        i, png = args
        slide_num = i + 1
        with httpx.Client() as client:
            vision = _vision_describe_slide(png, slide_num, total, client)
        entry = {
            "slide_number":       slide_num,
            "thumbnail_path":     str(png),
            "visual_description": vision.get("visual_description", ""),
            "layout_archetype":   vision.get("layout_archetype", "other"),
        }
        logger.info("  [%d/%d] %s — %s", slide_num, total,
                    entry["layout_archetype"], entry["visual_description"][:80])
        return i, entry

    logger.info("vision: analyzing %d slides (parallelism=%d) …", total, CATALOG_PARALLELISM)
    results: list[tuple[int, dict]] = []
    with ThreadPoolExecutor(max_workers=CATALOG_PARALLELISM) as pool:
        futures = {pool.submit(_process, (i, slide_pngs[i])): i for i in range(total)}
        for future in as_completed(futures):
            results.append(future.result())

    slides = [entry for _, entry in sorted(results, key=lambda t: t[0])]

    # 4. Save
    catalog = {
        "template_name":  template_path.stem,
        "template_path":  str(template_path),
        "template_hash":  _file_hash(template_path),
        "slide_count":    total,
        "collage_path":   str(collage_path),
        "cataloged_at":   datetime.now(timezone.utc).isoformat(),
        "slides":         slides,
    }
    _save_catalog(catalog, template_path)
    logger.info("=== Done: %d slides cataloged ===", total)
    return catalog


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Catalog a PPTX template for the cosmic-slides-2 pipeline."
    )
    parser.add_argument("template", type=Path, help="Path to the .pptx template.")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Re-catalog even if an up-to-date catalog exists.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    try:
        catalog = catalog_template(args.template, force=args.force)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\nCatalog ready: {_catalog_path(args.template.resolve())}")
    print(f"  Slides  : {catalog['slide_count']}")
    print(f"  Collage : {catalog['collage_path']}")
    print()
    for s in catalog["slides"]:
        print(f"  [{s['slide_number']:02d}] {s['layout_archetype']:<20}  {s['visual_description'][:90]}…")


if __name__ == "__main__":
    _cli()
