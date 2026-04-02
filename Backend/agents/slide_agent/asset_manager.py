"""Asset manager — image resize/crop/format, diagram/image agent delegation.

Handles:
- Image resizing via Pillow
- Aspect ratio cropping
- Format conversion (RGBA→RGB for PPTX compatibility)
- Delegation to diagram agent and image generator agent
- Asset loading from docs_parser bundles
"""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

# Standard slide aspect ratios
SLIDE_16_9 = (16, 9)
SLIDE_4_3 = (4, 3)


def resize_image(
    image_bytes: bytes,
    *,
    target_width_px: int,
    target_height_px: int,
    maintain_aspect: bool = True,
) -> bytes:
    """Resize image to target dimensions. Returns PNG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")

    if maintain_aspect:
        img.thumbnail((target_width_px, target_height_px), Image.LANCZOS)
    else:
        img = img.resize((target_width_px, target_height_px), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def crop_to_aspect(
    image_bytes: bytes,
    *,
    aspect_ratio: tuple[int, int] = (16, 9),
) -> bytes:
    """Crop image to target aspect ratio (center crop). Returns PNG bytes."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")

    target_w, target_h = aspect_ratio
    target_ratio = target_w / target_h
    img_ratio = img.width / img.height

    if img_ratio > target_ratio:
        # Image is wider — crop horizontally
        new_width = int(img.height * target_ratio)
        left = (img.width - new_width) // 2
        img = img.crop((left, 0, left + new_width, img.height))
    elif img_ratio < target_ratio:
        # Image is taller — crop vertically
        new_height = int(img.width / target_ratio)
        top = (img.height - new_height) // 2
        img = img.crop((0, top, img.width, top + new_height))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def convert_image_format(
    image_bytes: bytes,
    *,
    output_format: str = "PNG",
) -> bytes:
    """Convert image format. Handles RGBA→RGB for PPTX compatibility."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA" and output_format.upper() in ("JPEG", "JPG"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format=output_format.upper())
    return buf.getvalue()


def get_image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """Return (width, height) in pixels."""
    img = Image.open(io.BytesIO(image_bytes))
    return img.width, img.height


def prepare_image_for_slide(
    image_bytes: bytes,
    *,
    target_width_inches: float,
    target_height_inches: float,
    dpi: int = 96,
) -> bytes:
    """Resize and prepare an image for slide placement.

    Converts inches to pixels using DPI, resizes, and ensures RGB mode.
    """
    target_w_px = int(target_width_inches * dpi)
    target_h_px = int(target_height_inches * dpi)

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")

    img = img.resize((target_w_px, target_h_px), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def load_image_from_path(path: Path) -> bytes:
    """Load image bytes from file path."""
    return path.read_bytes()


def save_temp_image(image_bytes: bytes, suffix: str = ".png") -> Path:
    """Save image to a temp file and return its path. Caller must clean up."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(image_bytes)
        return Path(f.name)
