"""Bounded Python execution helpers for the slide specialist.

This is a Python-level sandbox, not kernel/container isolation. It is intended
for low-risk local asset generation such as charts and diagrams, with explicit
filesystem bounds, isolated Python mode, optional package provisioning, and
execution receipts. Do not treat it as safe for hostile code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

_MAX_SLIDE_SCRIPT_BYTES = 256_000
_MAX_PIP_PACKAGES = 12
_MAX_PACKAGE_NAME_LEN = 80
_ENV_SANDBOX_ROOT = "COSMIC_SLIDE_SANDBOX_ROOT"

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

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(\[.+\])?$")
_PACKAGE_DENY = frozenset(
    {
        "subprocess",
        "os",
        "sys",
        "ctypes",
        "multiprocessing",
        "pty",
        "pip",
        "setuptools",
        "wheel",
        "distlib",
        "ensurepip",
    }
)


def _deny_patterns(*, allow_network: bool) -> tuple[re.Pattern[str], ...]:
    if allow_network:
        return _DENY_CORE
    return _DENY_CORE + _DENY_NETWORK


_SLIDE_FS_PRELUDE = '''
# --- COSMIC slide sandbox prelude (do not edit or remove) ---
from __future__ import annotations

import builtins as _bi
import io as _io
import os as _os
import shutil as _sh
import sys as _sys
from pathlib import Path as _Path

def _root() -> _Path:
    raw = _os.environ.get("COSMIC_SLIDE_SANDBOX_ROOT", "").strip()
    if not raw:
        raise RuntimeError("COSMIC_SLIDE_SANDBOX_ROOT is not set")
    return _Path(raw).resolve()

_ROOT = _root()

def _allowed_import_roots() -> tuple[_Path, ...]:
    roots: list[_Path] = []
    for raw in list(_sys.path) + [getattr(_sys, "prefix", ""), getattr(_sys, "base_prefix", "")]:
        if not raw:
            continue
        try:
            candidate = _Path(raw).resolve()
        except Exception:
            continue
        try:
            candidate.relative_to(_ROOT)
            continue
        except ValueError:
            pass
        if candidate.exists():
            roots.append(candidate)
    return tuple(dict.fromkeys(roots))

_ALLOWED_IMPORT_ROOTS = _allowed_import_roots()

def _is_under(path: _Path, root: _Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False

def _is_read_only_mode(mode: str) -> bool:
    raw = str(mode or "r")
    return not any(flag in raw for flag in ("w", "a", "x", "+"))

def _must_under(p: _Path, *, mode: str = "r") -> _Path:
    r = p.resolve()
    if _is_under(r, _ROOT):
        return r
    if _is_read_only_mode(mode):
        for import_root in _ALLOWED_IMPORT_ROOTS:
            if _is_under(r, import_root):
                return r
    raise PermissionError("slide_sandbox: path escapes sandbox root: " + str(r))

def _p(arg, *, mode: str = "r") -> _Path:
    if isinstance(arg, _Path):
        q = arg
    else:
        q = _Path(str(arg))
    if not q.is_absolute():
        q = (_Path.cwd() / q).resolve()
    else:
        q = q.resolve()
    return _must_under(q, mode=mode)

_real_open = _bi.open
def _safe_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
    if isinstance(file, int):
        raise PermissionError("slide_sandbox: integer file descriptors are not allowed")
    p = _p(file, mode=str(mode or "r"))
    return _real_open(str(p), mode, buffering, encoding, errors, newline)

_bi.open = _safe_open
_io.open = _safe_open

_real_remove = _os.remove
def _safe_remove(path, *a, **k):
    return _real_remove(str(_p(path, mode="w")), *a, **k)
_os.remove = _safe_remove
_os.unlink = _safe_remove

_real_rename = _os.rename
def _safe_rename(src, dst, *a, **k):
    return _real_rename(str(_p(src, mode="w")), str(_p(dst, mode="w")), *a, **k)
_os.rename = _safe_rename

_real_replace = _os.replace
def _safe_replace(src, dst, *a, **k):
    return _real_replace(str(_p(src, mode="w")), str(_p(dst, mode="w")), *a, **k)
_os.replace = _safe_replace

_real_mkdir = _os.mkdir
def _safe_mkdir(path, mode=0o777, *, dir_fd=None):
    if dir_fd is not None:
        raise PermissionError("slide_sandbox: dir_fd not allowed")
    return _real_mkdir(str(_p(path, mode="w")), mode)
_os.mkdir = _safe_mkdir

_real_makedirs = _os.makedirs
def _safe_makedirs(name, mode=0o777, exist_ok=False):
    return _real_makedirs(str(_p(name, mode="w")), mode, exist_ok=exist_ok)
_os.makedirs = _safe_makedirs

def _no_chdir(path):
    raise PermissionError("slide_sandbox: os.chdir is disabled")
_os.chdir = _no_chdir

_real_move = _sh.move
def _safe_move(src, dst, *a, **k):
    return _real_move(str(_p(src, mode="w")), str(_p(dst, mode="w")), *a, **k)
_sh.move = _safe_move

_real_copy = _sh.copy
def _safe_copy(src, dst, *a, **k):
    return _real_copy(str(_p(src, mode="r")), str(_p(dst, mode="w")), *a, **k)
_sh.copy = _safe_copy

_real_copy2 = _sh.copy2
def _safe_copy2(src, dst, *a, **k):
    return _real_copy2(str(_p(src, mode="r")), str(_p(dst, mode="w")), *a, **k)
_sh.copy2 = _safe_copy2

_real_rmtree = _sh.rmtree
def _safe_rmtree(path, *a, **k):
    return _real_rmtree(str(_p(path, mode="w")), *a, **k)
_sh.rmtree = _safe_rmtree

_real_copytree = _sh.copytree
def _safe_copytree(src, dst, *a, **k):
    return _real_copytree(str(_p(src, mode="r")), str(_p(dst, mode="w")), *a, **k)
_sh.copytree = _safe_copytree

# --- end COSMIC slide sandbox prelude ---
'''.strip()


def validate_slide_python_code(
    code: str,
    *,
    allow_network: bool = False,
    max_script_bytes: int = _MAX_SLIDE_SCRIPT_BYTES,
) -> None:
    if not code or not str(code).strip():
        raise ValueError("empty python code")
    raw = str(code)
    if len(raw.encode("utf-8")) > max_script_bytes:
        raise ValueError("python code exceeds size limit")
    for pattern in _deny_patterns(allow_network=allow_network):
        if pattern.search(raw):
            raise ValueError(f"forbidden construct: {pattern.pattern}")


def bundle_script_with_prelude(
    *,
    user_code: str,
    allow_network: bool = False,
    max_script_bytes: int = _MAX_SLIDE_SCRIPT_BYTES,
) -> str:
    validate_slide_python_code(
        user_code,
        allow_network=allow_network,
        max_script_bytes=max_script_bytes,
    )
    return f"{_SLIDE_FS_PRELUDE}\n\n# --- user code ---\n{user_code.strip()}\n"


def persist_slide_python_script(
    *,
    sandbox_root: Path,
    execution_id: str,
    code: str,
    allow_network: bool = False,
    max_script_bytes: int = _MAX_SLIDE_SCRIPT_BYTES,
) -> Path:
    sandbox_root = sandbox_root.resolve()
    full = bundle_script_with_prelude(
        user_code=code,
        allow_network=allow_network,
        max_script_bytes=max_script_bytes,
    )
    codes_dir = sandbox_root / "codes"
    codes_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", execution_id)[:120] or "exec"
    path = codes_dir / f"{safe_id}.py"
    path.write_text(full, encoding="utf-8")
    return path


def validate_pip_packages(packages: list[str]) -> list[str]:
    clean: list[str] = []
    for raw in packages[:_MAX_PIP_PACKAGES]:
        pkg = str(raw).strip()
        if not pkg or len(pkg) > _MAX_PACKAGE_NAME_LEN:
            continue
        name_only = re.split(r"[<>=!~\[]", pkg, maxsplit=1)[0].strip().lower()
        if name_only in _PACKAGE_DENY:
            raise ValueError(f"forbidden pip package: {name_only}")
        comparable = (
            pkg.split("==")[0]
            .split(">=")[0]
            .split("<=")[0]
            .split("<")[0]
            .split(">")[0]
            .strip()
        )
        if not _PACKAGE_NAME_RE.match(comparable):
            raise ValueError(f"invalid pip package specifier: {pkg}")
        clean.append(pkg)
    return clean


def _venv_key(packages: list[str]) -> str:
    canonical = sorted(item.strip().lower() for item in packages if item.strip())
    digest = hashlib.sha256("|".join(canonical).encode()).hexdigest()[:16]
    return f"slide_venv_{digest}"


def _build_isolated_env(
    *,
    sandbox_root: Path,
    home_root: Path,
    pip_cache_dir: Path | None = None,
) -> dict[str, str]:
    home_root = home_root.resolve()
    home_root.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {
        _ENV_SANDBOX_ROOT: str(sandbox_root.resolve()),
        "HOME": str(home_root),
        "USERPROFILE": str(home_root),
        "APPDATA": str(home_root / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home_root / "AppData" / "Local"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(home_root / ".matplotlib"),
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
    venv_python = venv_dir / "Scripts" / "python.exe" if sys.platform == "win32" else venv_dir / "bin" / "python"
    pip_log: dict[str, Any] = {"packages_requested": clean, "venv_dir": str(venv_dir)}

    if marker.is_file() and venv_python.is_file():
        pip_log["cache_hit"] = True
        return venv_python, clean, pip_log

    import venv as _venv_mod

    _venv_mod.create(str(venv_dir), with_pip=True, clear=True)
    if not venv_python.is_file():
        raise RuntimeError(f"venv creation failed: {venv_python} not found")

    started = time.perf_counter()
    pip_env = _build_isolated_env(
        sandbox_root=venv_dir,
        home_root=venv_dir / ".sandbox_home",
        pip_cache_dir=venv_dir / ".pip_cache",
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


def run_python_script(
    *,
    script_path: Path,
    cwd: Path,
    timeout_sec: float,
    sandbox_root: Path | None = None,
    python_executable: Path | str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    root = (sandbox_root if sandbox_root is not None else cwd).resolve()
    env = _build_isolated_env(
        sandbox_root=root,
        home_root=root / ".sandbox_home",
    )
    exe = str(python_executable) if python_executable else sys.executable
    try:
        proc = subprocess.run(
            [exe, "-I", str(script_path)],
            cwd=str(cwd.resolve()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
            env=env,
        )
        return {
            "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[:32_000],
            "stderr": (proc.stderr or "")[:32_000],
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"timeout_after_{timeout_sec}s",
            "duration_ms": int(timeout_sec * 1000),
        }


def write_execution_receipt(
    *,
    sandbox_root: Path,
    execution_id: str,
    receipt: dict[str, Any],
) -> Path:
    sandbox_root.mkdir(parents=True, exist_ok=True)
    (sandbox_root / "codes").mkdir(exist_ok=True)
    (sandbox_root / "executions").mkdir(exist_ok=True)
    path = sandbox_root / "executions" / f"{execution_id}.json"
    payload = {
        "execution_id": execution_id,
        **receipt,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
