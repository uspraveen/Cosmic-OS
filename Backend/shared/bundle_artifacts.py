"""Classification for source bundles and standalone web assets.

Follows the same shape as document_artifacts / image_artifacts / tabular_artifacts:
a frozen set of mimes, a frozen set of extensions, an inference helper, and a
predicate. The gateway's ``_supported_artifact_kind`` consults these in order,
so the ordering there is what keeps .docx (which is a zip) with the document
pipeline rather than here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .archive_safety import DOCUMENT_ZIP_EXTENSIONS, EXECUTABLE_ZIP_EXTENSIONS


SUPPORTED_BUNDLE_MIME_TYPES = frozenset(
    {
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    }
)

SUPPORTED_BUNDLE_EXTENSIONS = frozenset({".zip"})

# Files a person would hand to a builder agent as-is. Deliberately excludes
# .json and .md: both are plausibly data or prose, and guessing wrong routes an
# upload away from the pipeline that should have had it. The handler registry
# makes widening this later a one-line change.
SUPPORTED_WEB_ASSET_MIME_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
    }
)

SUPPORTED_WEB_ASSET_EXTENSIONS = frozenset(
    {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
)

_WEB_ASSET_MIME_BY_EXTENSION = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".vue": "text/plain",
    ".svelte": "text/plain",
}


def _artifact_mime(artifact: dict[str, Any]) -> str:
    return str(artifact.get("mime") or artifact.get("mime_type") or "").strip().lower()


def _artifact_extension(artifact: dict[str, Any]) -> str:
    filename = str(artifact.get("filename") or artifact.get("path") or "").strip()
    return Path(filename).suffix.lower() if filename else ""


def infer_bundle_mime_from_extension(extension: str) -> str:
    normalized = str(extension or "").strip().lower()
    return "application/zip" if normalized in SUPPORTED_BUNDLE_EXTENSIONS else "application/octet-stream"


def infer_web_asset_mime_from_extension(extension: str) -> str:
    normalized = str(extension or "").strip().lower()
    return _WEB_ASSET_MIME_BY_EXTENSION.get(normalized, "text/plain")


def is_supported_bundle_artifact(artifact: dict[str, Any] | None) -> bool:
    """True for archives Cosmic will unpack into a workspace.

    The extension is authoritative in the negative direction: a .docx carries a
    zip mime from some clients, and a .jar is a zip by construction. Neither may
    be claimed here no matter what the mime says.
    """
    if not isinstance(artifact, dict):
        return False
    extension = _artifact_extension(artifact)
    if extension in DOCUMENT_ZIP_EXTENSIONS or extension in EXECUTABLE_ZIP_EXTENSIONS:
        return False
    if extension in SUPPORTED_BUNDLE_EXTENSIONS:
        return True
    # A zip mime with no extension at all is still a bundle; with a *different*
    # known extension it is not, which the check above already settled.
    return not extension and _artifact_mime(artifact) in SUPPORTED_BUNDLE_MIME_TYPES


def is_supported_web_asset_artifact(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    extension = _artifact_extension(artifact)
    if extension:
        return extension in SUPPORTED_WEB_ASSET_EXTENSIONS
    mime = _artifact_mime(artifact).split(";", 1)[0].strip()
    return mime in SUPPORTED_WEB_ASSET_MIME_TYPES
