"""Code sandbox — run arbitrary Python code in a subprocess to produce artifacts.

Follows the tabular agent's sandbox pattern:
- Isolated subprocess execution
- pip install support for packages (matplotlib, seaborn, pandas, etc.)
- Captures output files (images, CSVs, data) from working directory
- Returns all generated files as artifact paths
- Proper environment isolation

Used by the slide agent for:
- Complex matplotlib/seaborn charts
- Data calculations and transforms
- Custom shape generation scripts
- Any computation that produces output files
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import venv as _venv_mod
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

# Packages that are pre-installed and don't need pip install
_DEFAULT_PACKAGES = {"matplotlib", "numpy", "pandas", "Pillow"}


def _venv_key(packages: list[str]) -> str:
    """Deterministic venv name from sorted package list."""
    import hashlib

    key = "|".join(sorted(p.strip().lower() for p in packages if p.strip()))
    return f"slide_venv_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def _build_isolated_env(
    *,
    bundle_root: Path,
    home_root: Path,
    pip_cache_dir: Path | None = None,
) -> dict[str, str]:
    """Build an isolated environment dict for subprocess execution."""
    env = dict(os.environ)
    env["HOME"] = str(home_root.resolve())
    env["USERPROFILE"] = str(home_root.resolve())
    env["MPLBACKEND"] = "Agg"
    env["MPLCONFIGDIR"] = str(bundle_root / ".mpl_config")
    env["PYTHONWARNINGS"] = "ignore"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if pip_cache_dir:
        env["PIP_CACHE_DIR"] = str(pip_cache_dir.resolve())
    return env


def validate_pip_packages(packages: list[str]) -> list[str]:
    """Sanitize package names — allow only alphanumeric, dash, underscore, dots."""
    import re

    out: list[str] = []
    for p in packages or []:
        s = str(p).strip()
        if re.fullmatch(r"[A-Za-z0-9_.\-\[\]>=<,!~^]+", s):
            out.append(s)
    return out


def provision_venv(
    *,
    packages: list[str],
    cache_root: Path | None = None,
    pip_timeout_sec: float = 120.0,
) -> tuple[Path, list[str], dict[str, Any]]:
    """Create or reuse a venv with the requested packages installed.

    Returns (venv_python_path, installed_packages, pip_log_dict).
    """
    clean = validate_pip_packages(packages)
    if not clean:
        raise ValueError("No valid packages to install.")

    venv_name = _venv_key(clean)
    if cache_root and str(cache_root).strip():
        venv_dir = Path(cache_root).resolve() / venv_name
    else:
        venv_dir = Path(tempfile.mkdtemp(prefix="cosmic_slide_venv_")) / venv_name

    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    marker = venv_dir / ".cosmic_venv_ready"

    if sys.platform == "win32":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"

    pip_log: dict[str, Any] = {"packages_requested": clean, "venv_dir": str(venv_dir)}

    if marker.is_file() and venv_python.is_file():
        pip_log["cache_hit"] = True
        return venv_python, clean, pip_log

    _venv_mod.create(str(venv_dir), with_pip=True, clear=True)

    if not venv_python.is_file():
        raise RuntimeError(f"venv creation failed: {venv_python} not found")

    started = time.perf_counter()
    home_root = venv_dir / ".sandbox_home"
    pip_cache_dir = venv_dir / ".pip_cache"
    pip_env = _build_isolated_env(
        bundle_root=venv_dir,
        home_root=home_root,
        pip_cache_dir=pip_cache_dir,
    )
    try:
        install_result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--no-input", "--quiet", *clean],
            capture_output=True,
            text=True,
            timeout=pip_timeout_sec,
            check=False,
            env=pip_env,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        pip_log["install_exit_code"] = install_result.returncode
        pip_log["install_duration_ms"] = duration_ms
        if install_result.returncode != 0:
            pip_log["install_stderr"] = install_result.stderr[:2000]
            logger.warning(
                "pip install failed for %s: %s", clean, install_result.stderr[:500]
            )
    except subprocess.TimeoutExpired:
        pip_log["install_timeout"] = True
        logger.warning("pip install timed out for %s", clean)
    except Exception as exc:
        pip_log["install_error"] = str(exc)[:500]

    # Mark as ready
    marker.write_text(json.dumps(pip_log))

    return venv_python, clean, pip_log


def run_sandbox(
    *,
    code: str,
    input_data: dict[str, Any] | None = None,
    output_dir: Path,
    packages: list[str] | None = None,
    venv_cache_root: Path | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Run Python code in an isolated sandbox. Collect output files.

    The code runs with:
    - CWD set to output_dir
    - `SANDBOX_INPUT` env var containing JSON of input_data
    - `SANDBOX_OUTPUT_DIR` env var pointing to output_dir
    - All files written to CWD are collected as artifacts

    Args:
        code: Python code to execute.
        input_data: Data dict available to the code via json.loads(os.environ['SANDBOX_INPUT']).
        output_dir: Working directory + where output files are collected.
        packages: Extra pip packages to install (matplotlib, seaborn, etc.).
        venv_cache_root: Cache directory for venvs.
        timeout_sec: Execution timeout.

    Returns:
        dict with:
            - success: bool
            - exit_code: int
            - stdout: str
            - stderr: str
            - duration_ms: int
            - output_files: list of {path, filename, size_bytes, mime}
            - venv_log: dict
    """
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine Python executable
    all_packages = list(set(_DEFAULT_PACKAGES | set(packages or [])))
    venv_log: dict[str, Any] = {}
    python_exe = sys.executable

    if packages:
        try:
            python_exe, _, venv_log = provision_venv(
                packages=packages,
                cache_root=venv_cache_root,
            )
        except Exception as exc:
            logger.warning("Failed to provision venv: %s, using system python", exc)
            venv_log["error"] = str(exc)[:500]

    # Write script
    script_path = output_dir / f"_sandbox_{uuid4().hex[:8]}.py"
    script_path.write_text(code, encoding="utf-8")

    # Build environment
    home_root = output_dir / ".sandbox_home"
    env = _build_isolated_env(bundle_root=output_dir, home_root=home_root)
    env["SANDBOX_INPUT"] = json.dumps(input_data or {})
    env["SANDBOX_OUTPUT_DIR"] = str(output_dir)

    # Snapshot files before execution
    before_files = set(output_dir.iterdir()) if output_dir.exists() else set()

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(python_exe), "-I", str(script_path)],
            cwd=str(output_dir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
            env=env,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        success = proc.returncode == 0
        stderr = (proc.stderr or "")[:16_000]
    except subprocess.TimeoutExpired:
        duration_ms = int(timeout_sec * 1000)
        success = False
        proc = None
        stderr = f"Sandbox timed out after {timeout_sec}s"

    # Collect output files (files created during execution)
    after_files = set(output_dir.iterdir()) if output_dir.exists() else set()
    new_files = after_files - before_files - {script_path}

    output_files: list[dict[str, Any]] = []
    for f in sorted(new_files):
        if f.is_file() and not f.name.startswith("_sandbox_") and f.name != "script.py":
            try:
                import mimetypes

                mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                output_files.append(
                    {
                        "path": str(f),
                        "filename": f.name,
                        "size_bytes": f.stat().st_size,
                        "mime": mime,
                    }
                )
            except Exception:
                pass

    # Clean up script
    try:
        script_path.unlink()
    except Exception:
        pass

    return {
        "success": success,
        "exit_code": proc.returncode if proc else -1,
        "stdout": (proc.stdout if proc else "")[:16_000],
        "stderr": stderr,
        "duration_ms": duration_ms,
        "output_files": output_files,
        "venv_log": venv_log,
    }


# ── Convenience functions for common chart types ───────────────────────


def generate_chart(
    *,
    chart_code: str,
    data: dict[str, Any] | None = None,
    output_dir: Path,
    width_inches: float = 10,
    height_inches: float = 5.625,
    dpi: int = 150,
    packages: list[str] | None = None,
) -> dict[str, Any]:
    """Generate a chart from matplotlib/seaborn code.

    Injects width_inches, height_inches, dpi as variables in the code.
    Returns the first output PNG file's path and bytes.
    """
    wrapper = f"""\
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import os, json

WIDTH_INCHES = {width_inches}
HEIGHT_INCHES = {height_inches}
DPI = {dpi}
DATA = json.loads(os.environ.get('SANDBOX_INPUT', '{{}}'))
OUTPUT_DIR = os.environ.get('SANDBOX_OUTPUT_DIR', '.')

plt.figure(figsize=(WIDTH_INCHES, HEIGHT_INCHES))

# --- User code ---
{chart_code}
# --- End user code ---

output_path = os.path.join(OUTPUT_DIR, 'chart.png')
plt.tight_layout()
plt.savefig(output_path, dpi=DPI, bbox_inches='tight', facecolor='white')
plt.close()
print(f'Chart saved to {{output_path}}')
"""

    result = run_sandbox(
        code=wrapper,
        input_data=data,
        output_dir=output_dir,
        packages=packages or ["matplotlib"],
    )

    # Find the chart PNG
    chart_bytes = b""
    chart_path = ""
    for f in result.get("output_files", []):
        if f["filename"].endswith(".png"):
            chart_path = f["path"]
            try:
                chart_bytes = Path(f["path"]).read_bytes()
            except Exception:
                pass
            break

    result["chart_path"] = chart_path
    result["chart_bytes"] = chart_bytes
    return result
