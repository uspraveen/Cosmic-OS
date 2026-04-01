"""Diagram renderers — CLI subprocess wrappers for Mermaid (mmdc), D2, and Excalidraw.

All rendering is local on the VM. No external APIs.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when a renderer fails to produce output."""

    def __init__(self, renderer: str, message: str, stderr: str = "") -> None:
        self.renderer = renderer
        self.stderr = stderr
        super().__init__(f"[{renderer}] {message}")


async def render_mermaid(
    definition: str,
    *,
    mmdc_path: str = "mmdc",
    output_format: str = "svg",
    background: str = "white",
    theme: str = "default",
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Render Mermaid definition to SVG/PNG via mmdc CLI.

    Returns dict with: output_path, output_format, source_path
    """
    with tempfile.TemporaryDirectory(prefix="diagram_mermaid_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.mmd"
        input_path.write_text(definition, encoding="utf-8")

        if output_path is None:
            ext = "png" if output_format == "png" else "svg"
            output_path = tmpdir_path / f"output.{ext}"

        cmd = [
            mmdc_path,
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "-b",
            background,
            "-t",
            theme,
        ]
        if output_format == "png":
            cmd.extend(["-s", "2"])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            raise RenderError(
                "mermaid",
                f"mmdc not found at '{mmdc_path}'. Install with: npm i -g @mermaid-js/mermaid-cli",
            )
        except subprocess.TimeoutExpired:
            raise RenderError("mermaid", "Mermaid rendering timed out (60s).")

        if result.returncode != 0:
            raise RenderError(
                "mermaid",
                f"mmdc exited with code {result.returncode}",
                stderr=result.stderr,
            )

        if not output_path.exists():
            raise RenderError(
                "mermaid",
                f"mmdc did not produce output file: {output_path}",
                stderr=result.stderr,
            )

        return {
            "output_path": output_path,
            "output_format": output_format,
            "source_path": input_path,
            "content": output_path.read_bytes(),
        }


async def render_d2(
    definition: str,
    *,
    d2_path: str = "d2",
    output_format: str = "svg",
    sketch: bool = False,
    pad: int = 100,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Render D2 definition to SVG/PNG via d2 CLI.

    Returns dict with: output_path, output_format, source_path
    """
    with tempfile.TemporaryDirectory(prefix="diagram_d2_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        input_path = tmpdir_path / "input.d2"
        input_path.write_text(definition, encoding="utf-8")

        if output_path is None:
            ext = "png" if output_format == "png" else "svg"
            output_path = tmpdir_path / f"output.{ext}"

        cmd = [d2_path]
        if sketch:
            cmd.append("--sketch")
        if pad:
            cmd.extend(["--pad", str(pad)])
        if output_format == "png":
            cmd.append("--png")
        cmd.extend([str(input_path), str(output_path)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            raise RenderError(
                "d2",
                f"d2 not found at '{d2_path}'. Install with: go install oss.terrastruct.com/d2@latest",
            )
        except subprocess.TimeoutExpired:
            raise RenderError("d2", "D2 rendering timed out (60s).")

        if result.returncode != 0:
            raise RenderError(
                "d2",
                f"d2 exited with code {result.returncode}",
                stderr=result.stderr,
            )

        if not output_path.exists():
            raise RenderError(
                "d2",
                f"d2 did not produce output file: {output_path}",
                stderr=result.stderr,
            )

        return {
            "output_path": output_path,
            "output_format": output_format,
            "source_path": input_path,
            "content": output_path.read_bytes(),
        }


def render_excalidraw(
    definition: str,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Write Excalidraw JSON definition. No CLI needed — the desktop renders it.

    Returns dict with: output_path, output_format, source_path, content
    """
    # Validate JSON
    try:
        parsed = json.loads(definition)
    except json.JSONDecodeError as exc:
        raise RenderError("excalidraw", f"Invalid Excalidraw JSON: {exc}")

    if not isinstance(parsed, dict) or "elements" not in parsed:
        # Auto-wrap if just an elements array
        if isinstance(parsed, list):
            parsed = {
                "type": "excalidraw",
                "version": 2,
                "source": "cosmic/diagram-agent",
                "elements": parsed,
            }
        else:
            raise RenderError(
                "excalidraw",
                "Excalidraw definition must be a JSON object with 'elements' key or a JSON array of elements.",
            )

    definition = json.dumps(parsed, indent=2, ensure_ascii=False)

    if output_path is None:
        with tempfile.NamedTemporaryFile(
            suffix=".excalidraw", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(definition)
            output_path = Path(f.name)
    else:
        output_path.write_text(definition, encoding="utf-8")

    return {
        "output_path": output_path,
        "output_format": "excalidraw",
        "source_path": output_path,
        "content": definition.encode("utf-8"),
    }
