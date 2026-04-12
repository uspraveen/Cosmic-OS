"""Lightweight OOXML validation for generated PowerPoint files."""

from __future__ import annotations

import zipfile
from pathlib import Path

from pptx import Presentation

REQUIRED_PPTX_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "ppt/presentation.xml",
}


def validate_pptx(path: str | Path) -> dict:
    pptx_path = Path(path)
    result = {
        "path": str(pptx_path),
        "exists": pptx_path.exists(),
        "is_zip": False,
        "required_parts_present": False,
        "slide_count": 0,
        "errors": [],
    }
    if not pptx_path.exists():
        result["errors"].append("PPTX file does not exist.")
        return result

    try:
        with zipfile.ZipFile(pptx_path) as zf:
            result["is_zip"] = True
            names = set(zf.namelist())
            missing = sorted(REQUIRED_PPTX_PARTS - names)
            if missing:
                result["errors"].append(f"Missing OOXML parts: {', '.join(missing)}")
            else:
                result["required_parts_present"] = True
    except zipfile.BadZipFile:
        result["errors"].append("File is not a valid ZIP/OOXML package.")
        return result

    try:
        prs = Presentation(str(pptx_path))
        result["slide_count"] = len(prs.slides)
        if result["slide_count"] <= 0:
            result["errors"].append("Presentation contains no slides.")
    except Exception as exc:
        result["errors"].append(f"python-pptx could not open the file: {exc}")

    return result
