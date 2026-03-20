from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class RenderedOfficeDocument:
    rendered_pdf_path: Path
    backend: str


class OfficeDocumentRenderer:
    def __init__(self, *, binary_path: str = "soffice", timeout_sec: float = 180.0) -> None:
        self.binary_path = binary_path.strip() or "soffice"
        self.timeout_sec = max(10.0, float(timeout_sec))

    def render_to_pdf(self, *, source_path: Path, working_root: Path) -> RenderedOfficeDocument:
        executable = self._resolve_binary()
        job_root = (working_root / f"office-render-{uuid4().hex[:12]}").resolve()
        out_root = job_root / "out"
        profile_root = job_root / "profile"
        out_root.mkdir(parents=True, exist_ok=True)
        profile_root.mkdir(parents=True, exist_ok=True)

        command = [
            executable,
            f"-env:UserInstallation={profile_root.as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--norestore",
            "--nolockcheck",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_root),
            str(source_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=self.timeout_sec,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Office renderer executable was not found: {self.binary_path}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Office renderer timed out after {self.timeout_sec:.0f}s.") from exc
        except subprocess.CalledProcessError as exc:
            output = "\n".join(
                part.strip()
                for part in (exc.stdout or "", exc.stderr or "")
                if part and part.strip()
            ).strip()
            details = output or f"exit code {exc.returncode}"
            raise RuntimeError(f"Office renderer failed: {details}") from exc

        rendered_pdf_path = out_root / f"{source_path.stem}.pdf"
        if not rendered_pdf_path.exists() or not rendered_pdf_path.is_file():
            output = "\n".join(
                part.strip()
                for part in (completed.stdout or "", completed.stderr or "")
                if part and part.strip()
            ).strip()
            raise RuntimeError(
                "Office renderer did not produce a PDF output."
                + (f" Details: {output}" if output else "")
            )
        return RenderedOfficeDocument(
            rendered_pdf_path=rendered_pdf_path.resolve(),
            backend="libreoffice-soffice",
        )

    def _resolve_binary(self) -> str:
        candidate = Path(self.binary_path).expanduser()
        if candidate.is_absolute():
            if candidate.exists() and candidate.is_file():
                return str(candidate.resolve())
            raise RuntimeError(f"Office renderer executable was not found: {candidate}")
        resolved = shutil.which(self.binary_path)
        if resolved:
            return resolved
        raise RuntimeError(f"Office renderer executable was not found: {self.binary_path}")
