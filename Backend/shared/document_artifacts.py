from __future__ import annotations

from pathlib import Path
from typing import Any


SUPPORTED_DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

SUPPORTED_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".pptx"})


def infer_document_mime_from_extension(extension: str) -> str:
    normalized_extension = str(extension or "").strip().lower()
    mapping = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
    return mapping.get(normalized_extension, "application/octet-stream")


def is_supported_document_artifact(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    mime = str(artifact.get("mime") or artifact.get("mime_type") or "").strip().lower()
    filename = str(artifact.get("filename") or artifact.get("path") or "").strip()
    extension = Path(filename).suffix.lower() if filename else ""
    if mime in SUPPORTED_DOCUMENT_MIME_TYPES:
        return True
    return extension in SUPPORTED_DOCUMENT_EXTENSIONS

