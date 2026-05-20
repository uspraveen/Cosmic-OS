from __future__ import annotations

from pathlib import Path
from typing import Any

COSMIC_MAP_MIME_TYPE = "application/vnd.cosmic.map+json"
COSMIC_MAP_EXTENSION = ".cosmic-map.json"

SUPPORTED_MAP_MIME_TYPES = frozenset(
    {
        COSMIC_MAP_MIME_TYPE,
        "application/geo+json",
    }
)

SUPPORTED_MAP_EXTENSIONS = frozenset({COSMIC_MAP_EXTENSION, ".geojson"})


def infer_map_mime_from_extension(extension: str) -> str:
    normalized_extension = str(extension or "").strip().lower()
    if normalized_extension in {".geojson"}:
        return "application/geo+json"
    if normalized_extension == COSMIC_MAP_EXTENSION:
        return COSMIC_MAP_MIME_TYPE
    return "application/octet-stream"


def is_supported_map_artifact(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    kind = str(artifact.get("kind") or "").strip().lower()
    if kind in {"map", "interactive_map", "cosmic_map"}:
        return True
    mime = str(artifact.get("mime") or artifact.get("mime_type") or "").strip().lower()
    filename = str(artifact.get("filename") or artifact.get("path") or "").strip()
    filename_lower = Path(filename).name.lower() if filename else ""
    extension = Path(filename).suffix.lower() if filename else ""
    if mime in SUPPORTED_MAP_MIME_TYPES:
        return True
    return filename_lower.endswith(COSMIC_MAP_EXTENSION) or extension in SUPPORTED_MAP_EXTENSIONS
