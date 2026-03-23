"""Bounded Python execution for tabular specialist (COSMIC-owned, not stock REPL).

Execution policy
-----------------
Every script runs under explicit COSMIC control:

- **Filesystem**: injected prelude patches ``open``/``io.open``/``os.*``/``shutil.*``
  so resolved paths must stay under the bundle root. Best-effort; native extensions may
  bypass (see ``COSMIC_TABULAR_SPREADSHEET_PIPELINE_PLAN.md``).
- **Network**: denied by default (regex denylist). When ``sandbox_allow_network=True``,
  the denylist relaxes network-related patterns and the receipt logs ``network_enabled=True``.
- **Packages**: denied by default. When ``sandbox_allow_pip=True``, a per-execution venv
  is provisioned under ``sandbox_venv_cache_root`` (or a temp dir), requested packages are
  installed explicitly, and the script runs inside that venv. Installed packages are logged
  in the execution receipt.
- **Timeout**: hard ``sandbox_timeout_sec`` for the script, ``sandbox_pip_timeout_sec`` for
  pip install.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_TABULAR_SCRIPT_BYTES = 256_000
_MAX_PIP_PACKAGES = 12
_MAX_PACKAGE_NAME_LEN = 80

_ENV_BUNDLE_ROOT = "COSMIC_TABULAR_BUNDLE_ROOT"

# ════════════════════════════════════════════════════════════
#  Deny patterns
# ════════════════════════════════════════════════════════════

_DENY_CORE: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsubprocess\b", re.I),
    re.compile(r"\bimport\s+os\b", re.I),
    re.compile(r"\bfrom\s+os\s+import", re.I),
    re.compile(r"\bimport\s+sys\b", re.I),
    re.compile(r"\bfrom\s+sys\s+import", re.I),
    re.compile(r"\beval\s*\(", re.I),
    re.compile(r"\bexec\s*\(", re.I),
    re.compile(r"__import__", re.I),
    re.compile(r"\bcompile\s*\(", re.I),
    re.compile(r"\bctypes\b", re.I),
    re.compile(r"\bmultiprocessing\b", re.I),
    re.compile(r"\bimport\s+builtins\b", re.I),
    re.compile(r"__builtins__", re.I),
    re.compile(r"\bos\.system\b", re.I),
    re.compile(r"\bos\.popen\b", re.I),
    re.compile(r"\bos\.spawn", re.I),
    re.compile(r"\bpty\b", re.I),
)

_DENY_NETWORK: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsocket\b", re.I),
    re.compile(r"\brequests\b", re.I),
    re.compile(r"\bhttpx\b", re.I),
    re.compile(r"\burllib\b", re.I),
)


def _deny_patterns(*, allow_network: bool) -> tuple[re.Pattern[str], ...]:
    if allow_network:
        return _DENY_CORE
    return _DENY_CORE + _DENY_NETWORK


# ════════════════════════════════════════════════════════════
#  Filesystem prelude
# ════════════════════════════════════════════════════════════

_TABULAR_FS_PRELUDE = '''
# --- COSMIC tabular sandbox prelude (do not edit or remove) ---
from __future__ import annotations

import builtins as _bi
import io as _io
import os as _os
import shutil as _sh
from pathlib import Path as _Path

def _root() -> _Path:
    raw = _os.environ.get("COSMIC_TABULAR_BUNDLE_ROOT", "").strip()
    if not raw:
        raise RuntimeError("COSMIC_TABULAR_BUNDLE_ROOT is not set")
    return _Path(raw).resolve()

_ROOT = _root()

def _must_under(p: _Path) -> _Path:
    r = p.resolve()
    try:
        r.relative_to(_ROOT)
    except ValueError:
        raise PermissionError("tabular_sandbox: path escapes bundle root: " + str(r)) from None
    return r

def _p(arg) -> _Path:
    if isinstance(arg, _Path):
        q = arg
    else:
        q = _Path(str(arg))
    if not q.is_absolute():
        q = (_Path.cwd() / q).resolve()
    else:
        q = q.resolve()
    return _must_under(q)

_real_open = _bi.open
def _safe_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
    if isinstance(file, int):
        raise PermissionError("tabular_sandbox: integer file descriptors are not allowed")
    p = _p(file)
    return _real_open(str(p), mode, buffering, encoding, errors, newline)

_bi.open = _safe_open
_io.open = _safe_open

_real_remove = _os.remove
def _safe_remove(path, *a, **k):
    return _real_remove(str(_p(path)), *a, **k)
_os.remove = _safe_remove
_os.unlink = _safe_remove

_real_rename = _os.rename
def _safe_rename(src, dst, *a, **k):
    return _real_rename(str(_p(src)), str(_p(dst)), *a, **k)
_os.rename = _safe_rename

_real_replace = _os.replace
def _safe_replace(src, dst, *a, **k):
    return _real_replace(str(_p(src)), str(_p(dst)), *a, **k)
_os.replace = _safe_replace

_real_mkdir = _os.mkdir
def _safe_mkdir(path, mode=0o777, *, dir_fd=None):
    if dir_fd is not None:
        raise PermissionError("tabular_sandbox: dir_fd not allowed")
    return _real_mkdir(str(_p(path)), mode)
_os.mkdir = _safe_mkdir

_real_makedirs = _os.makedirs
def _safe_makedirs(name, mode=0o777, exist_ok=False):
    return _real_makedirs(str(_p(name)), mode, exist_ok=exist_ok)
_os.makedirs = _safe_makedirs

def _no_chdir(path):
    raise PermissionError("tabular_sandbox: os.chdir is disabled")
_os.chdir = _no_chdir

_real_move = _sh.move
def _safe_move(src, dst, *a, **k):
    return _real_move(str(_p(src)), str(_p(dst)), *a, **k)
_sh.move = _safe_move

_real_copy = _sh.copy
def _safe_copy(src, dst, *a, **k):
    return _real_copy(str(_p(src)), str(_p(dst)), *a, **k)
_sh.copy = _safe_copy

_real_copy2 = _sh.copy2
def _safe_copy2(src, dst, *a, **k):
    return _real_copy2(str(_p(src)), str(_p(dst)), *a, **k)
_sh.copy2 = _safe_copy2

_real_rmtree = _sh.rmtree
def _safe_rmtree(path, *a, **k):
    return _real_rmtree(str(_p(path)), *a, **k)
_sh.rmtree = _safe_rmtree

_real_copytree = _sh.copytree
def _safe_copytree(src, dst, *a, **k):
    return _real_copytree(str(_p(src)), str(_p(dst)), *a, **k)
_sh.copytree = _safe_copytree

# --- end COSMIC tabular sandbox prelude ---
'''.strip()


# ════════════════════════════════════════════════════════════
#  Validation + script assembly
# ════════════════════════════════════════════════════════════

def validate_tabular_python_code(code: str, *, allow_network: bool = False) -> None:
    """Reject unsafe patterns before persisting under the bundle (layer 1; prelude is layer 2)."""
    if not code or not str(code).strip():
        raise ValueError("empty python_code")
    raw = str(code)
    if len(raw.encode("utf-8")) > _MAX_TABULAR_SCRIPT_BYTES:
        raise ValueError("python_code exceeds size limit")
    for pat in _deny_patterns(allow_network=allow_network):
        if pat.search(raw):
            raise ValueError(f"forbidden construct: {pat.pattern}")


def bundle_script_with_prelude(*, user_code: str, allow_network: bool = False) -> str:
    """Return full script body: filesystem prelude + user code."""
    validate_tabular_python_code(user_code, allow_network=allow_network)
    return f"{_TABULAR_FS_PRELUDE}\n\n# --- user code ---\n{user_code.strip()}\n"


def persist_bundle_python_script(
    *,
    bundle_root: Path,
    execution_id: str,
    code: str,
    allow_network: bool = False,
) -> Path:
    """Write ``codes/<execution_id>.py`` under the workbook bundle (with sandbox prelude prepended)."""
    bundle_root = bundle_root.resolve()
    full = bundle_script_with_prelude(user_code=code, allow_network=allow_network)
    codes_dir = bundle_root / "codes"
    codes_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)[:120] or "exec"
    path = codes_dir / f"{safe_id}.py"
    path.write_text(full, encoding="utf-8")
    return path


# ════════════════════════════════════════════════════════════
#  Package validation
# ════════════════════════════════════════════════════════════

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(\[.+\])?$")
_PACKAGE_DENY = frozenset({
    "subprocess", "os", "sys", "ctypes", "multiprocessing", "pty",
    "pip", "setuptools", "wheel", "distlib", "ensurepip",
})


def validate_pip_packages(packages: list[str]) -> list[str]:
    """Normalize and validate a list of pip package specifiers. Returns clean list."""
    out: list[str] = []
    for raw in packages[:_MAX_PIP_PACKAGES]:
        pkg = str(raw).strip()
        if not pkg or len(pkg) > _MAX_PACKAGE_NAME_LEN:
            continue
        name_only = re.split(r"[<>=!~\[]", pkg, maxsplit=1)[0].strip().lower()
        if name_only in _PACKAGE_DENY:
            raise ValueError(f"forbidden pip package: {name_only}")
        if not _PACKAGE_NAME_RE.match(pkg.split("==")[0].split(">=")[0].split("<=")[0].split("<")[0].split(">")[0].strip()):
            raise ValueError(f"invalid pip package specifier: {pkg}")
        out.append(pkg)
    return out


# ════════════════════════════════════════════════════════════
#  Venv provisioning
# ════════════════════════════════════════════════════════════

def _venv_key(packages: list[str]) -> str:
    """Deterministic key for a set of packages so we can reuse a cached venv."""
    canonical = sorted(p.strip().lower() for p in packages if p.strip())
    digest = hashlib.sha256("|".join(canonical).encode()).hexdigest()[:16]
    return f"tabular_venv_{digest}"


def _build_isolated_env(
    *,
    bundle_root: Path,
    home_root: Path,
    pip_cache_dir: Path | None = None,
) -> dict[str, str]:
    """Build a minimal subprocess environment to avoid ambient host leakage."""
    home_root = home_root.resolve()
    home_root.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {
        _ENV_BUNDLE_ROOT: str(bundle_root.resolve()),
        "HOME": str(home_root),
        "USERPROFILE": str(home_root),
        "APPDATA": str(home_root / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home_root / "AppData" / "Local"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_CONFIG_FILE": os.devnull,
    }

    if pip_cache_dir is not None:
        pip_cache_dir = pip_cache_dir.resolve()
        pip_cache_dir.mkdir(parents=True, exist_ok=True)
        env["PIP_CACHE_DIR"] = str(pip_cache_dir)

    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "NUMBER_OF_PROCESSORS"):
        value = os.environ.get(key)
        if value:
            env[key] = value

    return env


def provision_venv(
    *,
    packages: list[str],
    cache_root: Path | None,
    pip_timeout_sec: float = 120.0,
) -> tuple[Path, list[str], dict[str, Any]]:
    """
    Create or reuse a venv with the requested packages installed.

    Returns (venv_python_path, installed_packages, pip_log_dict).
    """
    clean = validate_pip_packages(packages)
    if not clean:
        raise ValueError("No valid packages to install.")

    venv_name = _venv_key(clean)
    if cache_root and str(cache_root).strip():
        venv_dir = Path(cache_root).resolve() / venv_name
    else:
        venv_dir = Path(tempfile.mkdtemp(prefix="cosmic_tabular_venv_")) / venv_name

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

    import venv as _venv_mod
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
        proc = subprocess.run(
            [
                str(venv_python),
                "-I",
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-cache-dir",
                "--no-input",
                "--disable-pip-version-check",
                *clean,
            ],
            capture_output=True,
            text=True,
            timeout=pip_timeout_sec,
            check=False,
            cwd=str(venv_dir),
            env=pip_env,
        )
        pip_log["pip_exit_code"] = proc.returncode
        pip_log["pip_stdout"] = (proc.stdout or "")[:8000]
        pip_log["pip_stderr"] = (proc.stderr or "")[:4000]
        pip_log["pip_duration_ms"] = int((time.perf_counter() - started) * 1000)
        pip_log["cache_hit"] = False
        pip_log["environment_mode"] = "isolated_minimal"
        if proc.returncode != 0:
            raise RuntimeError(f"pip install failed (exit {proc.returncode}): {(proc.stderr or '')[:500]}")
    except subprocess.TimeoutExpired:
        pip_log["pip_exit_code"] = -1
        pip_log["pip_stderr"] = f"timeout_after_{pip_timeout_sec}s"
        pip_log["pip_duration_ms"] = int(pip_timeout_sec * 1000)
        pip_log["cache_hit"] = False
        raise RuntimeError(f"pip install timed out after {pip_timeout_sec}s") from None

    marker.write_text(json.dumps({"packages": clean}, ensure_ascii=False), encoding="utf-8")
    return venv_python, clean, pip_log


# ════════════════════════════════════════════════════════════
#  Script execution
# ════════════════════════════════════════════════════════════

def run_python_script(
    *,
    script_path: Path,
    cwd: Path,
    timeout_sec: float,
    bundle_root: Path | None = None,
    python_executable: Path | str | None = None,
) -> dict[str, Any]:
    """
    Run script with cwd set and ``COSMIC_TABULAR_BUNDLE_ROOT`` enforced for the sandbox prelude.

    Uses ``python -I`` (isolated, no user site) to reduce import side channels.
    When a venv python is provided, uses that instead of ``sys.executable``.
    """
    started = time.perf_counter()
    root = (bundle_root if bundle_root is not None else cwd).resolve()
    env = _build_isolated_env(
        bundle_root=root,
        home_root=root / ".sandbox_home",
    )
    cwd_resolved = cwd.resolve()
    exe = str(python_executable) if python_executable else sys.executable
    try:
        proc = subprocess.run(
            [exe, "-I", str(script_path)],
            cwd=str(cwd_resolved),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
            env=env,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        return {
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[:32_000],
            "stderr": (proc.stderr or "")[:32_000],
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"timeout_after_{timeout_sec}s",
            "duration_ms": int(timeout_sec * 1000),
        }


# ════════════════════════════════════════════════════════════
#  Execution receipts
# ════════════════════════════════════════════════════════════

def write_execution_receipt(
    *,
    bundle_root: Path,
    execution_id: str,
    task_id: str,
    session_id: str | None,
    artifact_id: str | None,
    receipt: dict[str, Any],
) -> Path:
    bundle_root.mkdir(parents=True, exist_ok=True)
    (bundle_root / "codes").mkdir(exist_ok=True)
    (bundle_root / "executions").mkdir(exist_ok=True)
    path = bundle_root / "executions" / f"{execution_id}.json"
    payload = {
        "execution_id": execution_id,
        "task_id": task_id,
        "session_id": session_id,
        "artifact_id": artifact_id,
        **receipt,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def materialize_temp_script(code: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(code)
        tmp.flush()
        return Path(tmp.name)
    finally:
        tmp.close()
