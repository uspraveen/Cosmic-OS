from __future__ import annotations

from pathlib import Path
from typing import Any


SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    }
)

SUPPORTED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"})


def infer_image_mime_from_extension(extension: str) -> str:
    normalized_extension = str(extension or "").strip().lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    return mapping.get(normalized_extension, "application/octet-stream")


def is_supported_image_artifact(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    mime = str(artifact.get("mime") or artifact.get("mime_type") or "").strip().lower()
    filename = str(artifact.get("filename") or artifact.get("path") or "").strip()
    extension = Path(filename).suffix.lower() if filename else ""
    if mime in SUPPORTED_IMAGE_MIME_TYPES:
        return True
    return extension in SUPPORTED_IMAGE_EXTENSIONS
