"""Asset manager — cosmic-slides-2.

Provides icons and stock photos for the slide builder.

Icons — Iconify (free, no API key required)
  search_icons(query, ...)        → list[IconResult]
  download_icon(icon_id, ...)     → Path  (PNG, cached)

Photos — Pexels (requires PEXELS_API_KEY in .env)
  search_photos(query, ...)       → list[PhotoResult]
  download_photo(photo, ...)      → Path  (JPEG, cached)

All downloads are cached under assets/cache/ and never re-fetched.

Dependencies
────────────
  pip install svglib reportlab         ← SVG → PNG conversion for icons
  pip install httpx python-dotenv      ← already in project

Usage
─────
  python asset_manager.py icons "electric vehicle charging"
  python asset_manager.py icons "team collaboration" --limit 8 --sets mdi fluent
  python asset_manager.py photos "india highway sunset" --orientation landscape
  python asset_manager.py photos "startup team meeting" --limit 5 --min-width 1920
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageColor, ImageOps

# ── Config ─────────────────────────────────────────────────────────────────────

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")

PEXELS_API_KEY: str    = os.getenv("PEXELS_API_KEY", "")
ASSETS_CACHE_DIR: Path = _HERE / os.getenv("ASSETS_CACHE_DIR", "assets/cache")
ICON_DEFAULT_SIZE: int = int(os.getenv("ICON_DEFAULT_SIZE_PX", "128"))
ICON_DEFAULT_COLOR: str = os.getenv("ICON_DEFAULT_COLOR", "#222222")

ICONIFY_API   = "https://api.iconify.design"
PEXELS_API    = "https://api.pexels.com/v1"
USER_AGENT    = "cosmic-slides-2/1.0"

logger = logging.getLogger(__name__)

_ICON_STOPWORDS = {
    "and", "or", "the", "for", "with", "from", "into", "over", "under",
    "a", "an", "of", "to", "in", "on", "by",
}

# ── Preferred icon sets — ordered by suitability for professional presentations ─

PREFERRED_ICON_SETS = [
    "fluent",           # Microsoft Fluent — polished, consistent weight
    "ph",               # Phosphor — beautiful dual-weight outlines
    "lucide",           # Lucide — clean, consistent stroke icons
    "tabler",           # Tabler — fine stroke, excellent coverage
    "mdi",              # Material Design Icons — huge, reliable set
    "carbon",           # IBM Carbon — sharp, enterprise-grade
    "heroicons",        # Heroicons — minimal, high-signal
    "bi",               # Bootstrap Icons — broad, clean
    "ri",               # Remix Icon — rounded, friendly
]

# Sets that should be excluded (emoji-only, brand logos, flags, etc.)
_EXCLUDE_SETS = {
    "twemoji", "noto-emoji", "emojione", "emojione-v1",
    "fxemoji", "openmoji", "noto", "fluent-emoji-flat",
    "logos", "simple-icons", "cib",   # brand logos
    "circle-flags", "flagpack",        # flags
    "game-icons",                      # gaming-specific
}

# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class IconResult:
    icon_id: str            # e.g. "mdi:home"
    prefix: str             # e.g. "mdi"
    name: str               # e.g. "home"
    collection_name: str    # e.g. "Material Design Icons"
    svg_url: str            # ready-to-fetch URL

    @property
    def preference_rank(self) -> int:
        """Lower = more preferred. Sets not in PREFERRED_ICON_SETS get rank 999."""
        try:
            return PREFERRED_ICON_SETS.index(self.prefix)
        except ValueError:
            return 999


@dataclass
class PhotoResult:
    photo_id: int
    alt: str
    photographer: str
    photographer_url: str
    width: int
    height: int
    avg_color: str
    src: dict[str, str]     # size_label → URL (original, large2x, large, medium, …)

    @property
    def aspect_ratio(self) -> float:
        return self.width / max(self.height, 1)

    @property
    def is_landscape(self) -> bool:
        return self.aspect_ratio >= 1.2


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _icon_cache_path(icon_id: str, size_px: int, color: str) -> Path:
    safe_id  = icon_id.replace(":", "_").replace("/", "_")
    safe_col = color.lstrip("#")
    return ASSETS_CACHE_DIR / "icons" / f"{safe_id}_{size_px}_{safe_col}_v2.png"


def _photo_cache_path(photo_id: int, size: str) -> Path:
    return ASSETS_CACHE_DIR / "photos" / f"{photo_id}_{size}.jpg"


def _ensure_cache() -> None:
    (ASSETS_CACHE_DIR / "icons").mkdir(parents=True, exist_ok=True)
    (ASSETS_CACHE_DIR / "photos").mkdir(parents=True, exist_ok=True)


# ── SVG → PNG conversion ───────────────────────────────────────────────────────

def _svg_bytes_to_png(svg_bytes: bytes, size_px: int) -> bytes:
    """Convert SVG bytes to PNG bytes at size_px × size_px.

    Uses svglib + reportlab (pure Python, no system dependencies).
    Install with: pip install svglib reportlab
    """
    try:
        import io
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPM
    except ImportError:
        raise ImportError(
            "SVG→PNG conversion requires svglib and reportlab.\n"
            "Install with: pip install svglib reportlab"
        ) from None

    import io
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM

    drawing = svg2rlg(io.BytesIO(svg_bytes))
    if drawing is None:
        raise ValueError("svglib could not parse the SVG.")

    # Scale to desired size
    scale = size_px / max(drawing.width, drawing.height, 1)
    drawing.width  = drawing.width  * scale
    drawing.height = drawing.height * scale
    drawing.transform = (scale, 0, 0, scale, 0, 0)

    buf = io.BytesIO()
    renderPM.drawToFile(drawing, buf, fmt="PNG")
    return buf.getvalue()


def _recolor_icon_png(png_bytes: bytes, color: str) -> bytes:
    """Convert a rendered icon PNG into a transparent, tinted asset.

    svglib/reportlab often rasterizes the icon onto an opaque white background.
    We convert luminance into alpha so the background disappears, then tint the
    icon to the requested color.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    grayscale = ImageOps.grayscale(img)
    alpha = ImageOps.invert(grayscale)
    rgb = ImageColor.getrgb(color)

    tinted = Image.new("RGBA", img.size, rgb + (0,))
    tinted.putalpha(alpha)

    out = io.BytesIO()
    tinted.save(out, format="PNG")
    return out.getvalue()


# ── Icons — Iconify ────────────────────────────────────────────────────────────

def search_icons(
    query: str,
    *,
    limit: int = 8,
    sets_filter: list[str] | None = None,
) -> list[IconResult]:
    """Search Iconify for icons matching query.

    Args:
        query:       Natural language search (e.g. "electric car charging").
        limit:       Number of results to return (after re-ranking).
        sets_filter: If given, only return icons from these set prefixes.

    Returns:
        List of IconResult sorted by set preference (best first).
    """
    fetch_n = max(limit * 4, 40)

    # Try the full query first; if sparse, fall back to each keyword individually.
    # Iconify matches icon names literally so "electric vehicle charging" often
    # returns nothing while "charging", "electric", "car" each return many hits.
    raw_icons, collections = _iconify_fetch_raw(query, fetch_n)

    if len(raw_icons) < limit:
        # Keyword fallback — try each word that is long enough to be meaningful
        keywords = [w for w in query.split() if len(w) >= 4]
        for kw in keywords:
            extra, extra_cols = _iconify_fetch_raw(kw, fetch_n)
            raw_icons  = raw_icons + [i for i in extra if i not in raw_icons]
            collections = {**extra_cols, **collections}
            if len(raw_icons) >= fetch_n:
                break
        logger.debug("search_icons: keyword fallback for '%s' → %d candidates", query, len(raw_icons))

    seen: set[str] = set()
    results: list[IconResult] = []
    for rank, icon_id in enumerate(raw_icons):
        if ":" not in icon_id or icon_id in seen:
            continue
        prefix, name = icon_id.split(":", 1)

        if prefix in _EXCLUDE_SETS:
            continue
        if sets_filter and prefix not in sets_filter:
            continue

        seen.add(icon_id)
        coll_name = (collections.get(prefix) or {}).get("name", prefix)
        results.append(IconResult(
            icon_id         = icon_id,
            prefix          = prefix,
            name            = name,
            collection_name = coll_name,
            svg_url         = (
                f"{ICONIFY_API}/{urllib.parse.quote(prefix)}"
                f"/{urllib.parse.quote(name)}.svg"
            ),
        ))

    # Sort: preferred sets first, preserve original API order within same tier
    indexed = list(enumerate(results))
    indexed.sort(key=lambda t: (t[1].preference_rank, t[0]))
    results = [r for _, r in indexed]

    logger.debug("search_icons('%s'): %d raw → %d after filter/rank", query, len(raw_icons), len(results))
    return results[:limit]


def _iconify_fetch_raw(query: str, fetch_n: int) -> tuple[list[str], dict]:
    """Single Iconify /search call. Returns (icon_ids, collections)."""
    params = urllib.parse.urlencode({"query": query, "limit": str(fetch_n)})
    url    = f"{ICONIFY_API}/search?{params}"
    req    = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Iconify API error {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Iconify connection failed: {exc.reason}") from exc
    return data.get("icons") or [], data.get("collections") or {}


def download_icon(
    icon_id: str,
    *,
    size_px: int | None = None,
    color: str | None = None,
) -> Path:
    """Download an Iconify icon as a PNG file.

    Returns the path to the cached PNG. Re-uses cache on subsequent calls.

    Args:
        icon_id:  Iconify icon ID, e.g. "mdi:home".
        size_px:  Output PNG size in pixels (square). Defaults to ICON_DEFAULT_SIZE.
        color:    Hex color for the icon, e.g. "#ffffff". Defaults to ICON_DEFAULT_COLOR.
    """
    size_px = size_px or ICON_DEFAULT_SIZE
    color   = color   or ICON_DEFAULT_COLOR

    _ensure_cache()
    cache_path = _icon_cache_path(icon_id, size_px, color)
    if cache_path.exists():
        logger.debug("icon cache hit: %s", cache_path)
        return cache_path

    if ":" not in icon_id:
        raise ValueError(f"Invalid icon_id '{icon_id}' — expected 'prefix:name' format.")
    prefix, name = icon_id.split(":", 1)

    # Fetch raw SVG, then tint after rasterization so the final PNG keeps a
    # transparent background instead of inheriting reportlab's white canvas.
    svg_url = (
        f"{ICONIFY_API}/{urllib.parse.quote(prefix)}"
        f"/{urllib.parse.quote(name)}.svg"
    )
    req = urllib.request.Request(svg_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            svg_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch SVG for '{icon_id}': HTTP {exc.code}") from exc

    if not svg_bytes.strip().startswith(b"<"):
        raise ValueError(f"Iconify returned non-SVG content for '{icon_id}'.")

    png_bytes = _recolor_icon_png(_svg_bytes_to_png(svg_bytes, size_px), color)
    cache_path.write_bytes(png_bytes)
    logger.info("icon downloaded: %s → %s", icon_id, cache_path)
    return cache_path


# ── Photos — Pexels ───────────────────────────────────────────────────────────

def search_photos(
    query: str,
    *,
    limit: int = 6,
    orientation: str = "landscape",
    min_width: int = 1280,
) -> list[PhotoResult]:
    """Search Pexels for stock photos matching query.

    Args:
        query:       Natural language search (e.g. "electric vehicles india highway").
        limit:       Max number of results to return.
        orientation: "landscape" | "portrait" | "square". Use "landscape" for slides.
        min_width:   Minimum photo width in pixels. Filters out low-res results.

    Returns:
        List of PhotoResult sorted by resolution (largest first).
    """
    if not PEXELS_API_KEY:
        raise ValueError(
            "PEXELS_API_KEY is not set.\n"
            "Get a free key at https://www.pexels.com/api/ and add it to .env"
        )

    params = {
        "query":       query,
        "per_page":    min(limit * 3, 30),   # fetch extra for filtering
        "orientation": orientation,
        "size":        "large",               # Pexels: filters for >4MP photos
    }

    with httpx.Client(timeout=20) as client:
        resp = client.get(
            f"{PEXELS_API}/search",
            params=params,
            headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent":    USER_AGENT,
            },
        )
        if resp.status_code == 401:
            raise ValueError("Pexels API key is invalid or expired.")
        if resp.status_code == 429:
            raise RuntimeError("Pexels rate limit reached. Try again shortly.")
        if resp.status_code >= 400:
            raise RuntimeError(f"Pexels API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

    raw_photos = data.get("photos") or []
    results: list[PhotoResult] = []

    for p in raw_photos:
        w = p.get("width", 0)
        h = p.get("height", 0)
        if w < min_width:
            continue
        results.append(PhotoResult(
            photo_id         = p["id"],
            alt              = p.get("alt", ""),
            photographer     = p.get("photographer", ""),
            photographer_url = p.get("photographer_url", ""),
            width            = w,
            height           = h,
            avg_color        = p.get("avg_color", ""),
            src              = p.get("src", {}),
        ))

    # Sort by resolution — highest quality first
    results.sort(key=lambda r: r.width * r.height, reverse=True)

    logger.debug(
        "search_photos('%s'): %d raw → %d after min_width=%d filter",
        query, len(raw_photos), len(results), min_width,
    )
    return results[:limit]


def download_photo(
    photo: PhotoResult,
    *,
    size: str = "large2x",
) -> Path:
    """Download a Pexels photo to the local cache.

    Args:
        photo:  PhotoResult from search_photos().
        size:   "original" | "large2x" | "large" | "medium" | "small".
                Use "large2x" (typically ~940×627px) for slides.
                Use "original" for full-bleed hero images.

    Returns:
        Path to the cached JPEG file.
    """
    _ensure_cache()
    cache_path = _photo_cache_path(photo.photo_id, size)
    if cache_path.exists():
        logger.debug("photo cache hit: %s", cache_path)
        return cache_path

    url = photo.src.get(size) or photo.src.get("large2x") or photo.src.get("large")
    if not url:
        raise ValueError(f"No URL available for photo {photo.photo_id} at size '{size}'.")

    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            raise RuntimeError(f"Failed to download photo {photo.photo_id}: HTTP {resp.status_code}")
        image_bytes = resp.content

    cache_path.write_bytes(image_bytes)
    logger.info(
        "photo downloaded: id=%d size=%s (%dx%d) → %s",
        photo.photo_id, size, photo.width, photo.height, cache_path,
    )
    return cache_path


# ── High-level convenience ────────────────────────────────────────────────────

def resolve_icon(
    description: str,
    *,
    size_px: int | None = None,
    color: str | None = None,
    sets_filter: list[str] | None = None,
) -> Path | None:
    """Find and download the best icon for a description.

    Searches Iconify, picks the top result from a preferred set, downloads
    as PNG. Returns the cached path, or None if nothing found.
    """
    def _query_variants(text: str) -> list[str]:
        raw = " ".join((text or "").split()).strip()
        if not raw:
            return []
        variants: list[str] = [raw]
        words = [w for w in raw.split() if w.lower() not in _ICON_STOPWORDS]
        if len(words) >= 2:
            variants.append(" ".join(words))
            variants.append(" ".join(words[:2]))
        variants.extend(w for w in words if len(w) >= 4)
        deduped: list[str] = []
        seen: set[str] = set()
        for item in variants:
            key = item.lower()
            if item and key not in seen:
                deduped.append(item)
                seen.add(key)
        return deduped

    candidates: list[IconResult] = []
    seen_icons: set[str] = set()
    for query in _query_variants(description):
        try:
            results = search_icons(query, limit=8, sets_filter=sets_filter)
        except RuntimeError as exc:
            logger.warning("resolve_icon('%s'): query '%s' failed: %s", description, query, exc)
            continue
        for result in results:
            if result.icon_id not in seen_icons:
                candidates.append(result)
                seen_icons.add(result.icon_id)
        if len(candidates) >= 12:
            break

    if not candidates:
        logger.warning("resolve_icon('%s'): no results found", description)
        return None

    for candidate in candidates[:12]:
        try:
            logger.info("resolve_icon('%s'): trying %s (%s)", description, candidate.icon_id, candidate.collection_name)
            return download_icon(candidate.icon_id, size_px=size_px, color=color)
        except (ImportError, RuntimeError, ValueError) as exc:
            logger.warning("resolve_icon('%s'): %s failed: %s", description, candidate.icon_id, exc)
            continue

    logger.warning("resolve_icon('%s'): all candidates failed", description)
    return None


def resolve_photo(
    prompt: str,
    *,
    orientation: str = "landscape",
    size: str = "large2x",
    min_width: int = 1280,
) -> Path | None:
    """Find and download the best Pexels photo for a prompt.

    Returns the cached path, or None if nothing found or API key missing.
    """
    if not PEXELS_API_KEY:
        logger.warning("resolve_photo: PEXELS_API_KEY not set — skipping photo search")
        return None
    results = search_photos(prompt, limit=3, orientation=orientation, min_width=min_width)
    if not results:
        logger.warning("resolve_photo('%s'): no results found", prompt)
        return None
    best = results[0]
    logger.info(
        "resolve_photo('%s'): chose id=%d '%s' (%dx%d) by %s",
        prompt, best.photo_id, best.alt[:60], best.width, best.height, best.photographer,
    )
    return download_photo(best, size=size)


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli_icons(args: argparse.Namespace) -> int:
    query = " ".join(args.query)
    sets_filter = args.sets or None

    try:
        results = search_icons(query, limit=args.limit, sets_filter=sets_filter)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No icons found.")
        return 0

    print(f'Icons for "{query}" — {len(results)} result(s)\n')
    for i, r in enumerate(results, 1):
        print(f"  {i}. {r.icon_id:<40}  [{r.collection_name}]")
        print(f"     {r.svg_url}")

    if args.download:
        print()
        for r in results[:args.download]:
            try:
                path = download_icon(r.icon_id,
                                     size_px=args.size,
                                     color=args.color)
                print(f"  ✓ {r.icon_id} → {path}")
            except (ImportError, RuntimeError, ValueError) as exc:
                print(f"  ✗ {r.icon_id}: {exc}")
    return 0


def _cli_photos(args: argparse.Namespace) -> int:
    query = " ".join(args.query)

    try:
        results = search_photos(
            query,
            limit=args.limit,
            orientation=args.orientation,
            min_width=args.min_width,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not results:
        print("No photos found.")
        return 0

    print(f'Photos for "{query}" — {len(results)} result(s)\n')
    for i, p in enumerate(results, 1):
        print(f"  {i}. [{p.photo_id}] {p.alt[:65]}")
        print(f"     {p.width}×{p.height}  by {p.photographer}  avg_color={p.avg_color}")
        print(f"     large2x: {p.src.get('large2x', 'n/a')}")

    if args.download:
        print()
        for photo in results[:args.download]:
            try:
                path = download_photo(photo, size=args.size)
                print(f"  ✓ id={photo.photo_id} → {path}")
            except (RuntimeError, ValueError) as exc:
                print(f"  ✗ id={photo.photo_id}: {exc}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Asset manager — search and download icons and photos for slides.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── icons ──
    p_icons = sub.add_parser("icons", help="Search Iconify for icons.")
    p_icons.add_argument("query", nargs="+", help="Search keywords.")
    p_icons.add_argument("--limit", "-n", type=int, default=8,
                         help="Number of results (default: 8).")
    p_icons.add_argument("--sets", nargs="+", metavar="SET",
                         help="Filter to specific icon sets, e.g. mdi fluent tabler.")
    p_icons.add_argument("--download", "-d", type=int, default=0, metavar="N",
                         help="Download top N results as PNG.")
    p_icons.add_argument("--size", type=int, default=ICON_DEFAULT_SIZE,
                         help=f"PNG size in pixels (default: {ICON_DEFAULT_SIZE}).")
    p_icons.add_argument("--color", default=ICON_DEFAULT_COLOR,
                         help=f"Icon color hex (default: {ICON_DEFAULT_COLOR}).")

    # ── photos ──
    p_photos = sub.add_parser("photos", help="Search Pexels for stock photos.")
    p_photos.add_argument("query", nargs="+", help="Search keywords.")
    p_photos.add_argument("--limit", "-n", type=int, default=6,
                          help="Number of results (default: 6).")
    p_photos.add_argument("--orientation", default="landscape",
                          choices=["landscape", "portrait", "square"],
                          help="Photo orientation (default: landscape).")
    p_photos.add_argument("--min-width", type=int, default=1280,
                          help="Minimum photo width in pixels (default: 1280).")
    p_photos.add_argument("--download", "-d", type=int, default=0, metavar="N",
                          help="Download top N results.")
    p_photos.add_argument("--size", default="large2x",
                          choices=["original", "large2x", "large", "medium"],
                          help="Download size (default: large2x).")

    return parser


def main() -> int:
    parser = _build_parser()
    args   = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s  %(message)s",
    )

    if args.command == "icons":
        return _cli_icons(args)
    if args.command == "photos":
        return _cli_photos(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
