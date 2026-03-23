from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# User-facing sheet_id used in filenames and DuckDB view names (defense in depth vs path injection).
_SAFE_SHEET_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")


SUPPORTED_TABULAR_MIME_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
        "text/csv",
        "text/tab-separated-values",
        "application/csv",
        "text/plain",
    }
)

SUPPORTED_TABULAR_EXTENSIONS = frozenset({".xlsx", ".xlsb", ".csv", ".tsv"})


def infer_tabular_mime_from_extension(extension: str) -> str:
    normalized_extension = str(extension or "").strip().lower()
    mapping = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
    }
    return mapping.get(normalized_extension, "application/octet-stream")


def is_supported_tabular_artifact(artifact: dict[str, Any] | None) -> bool:
    if not isinstance(artifact, dict):
        return False
    mime = str(artifact.get("mime") or artifact.get("mime_type") or "").strip().lower()
    filename = str(artifact.get("filename") or artifact.get("path") or "").strip()
    extension = Path(filename).suffix.lower() if filename else ""
    if mime in SUPPORTED_TABULAR_MIME_TYPES:
        return True
    return extension in SUPPORTED_TABULAR_EXTENSIONS


def validate_safe_sheet_id(sheet_id: str) -> str:
    """
    Validate a sheet_id used in paths (Parquet, preview MD) and DuckDB identifiers.

    Allowed: letters, digits, underscore; length 1-80. Matches ``^[A-Za-z0-9_]{1,80}$``.
    Raises ValueError if invalid.
    """
    s = str(sheet_id or "").strip()
    if _SAFE_SHEET_ID_RE.fullmatch(s) is None:
        raise ValueError(
            "sheet_id must be 1-80 characters and match ^[A-Za-z0-9_]{1,80}$ (letters, digits, underscore only)."
        )
    return s
