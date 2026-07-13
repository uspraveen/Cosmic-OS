from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import venv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


_MAX_CAPTURE_CHARS = 32000
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.-]+([<>=!~]=?[A-Za-z0-9_.+-]+)?$")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|\n)\s*import\s+os\b"), "Direct os imports are blocked in the local code sandbox."),
    (re.compile(r"(^|\n)\s*from\s+os\s+import\b"), "Direct os imports are blocked in the local code sandbox."),
    (re.compile(r"(^|\n)\s*import\s+sys\b"), "Direct sys imports are blocked in the local code sandbox."),
    (re.compile(r"(^|\n)\s*from\s+sys\s+import\b"), "Direct sys imports are blocked in the local code sandbox."),
    (re.compile(r"\bsubprocess\b"), "Subprocess execution is blocked in the local code sandbox."),
    (re.compile(r"\b(?:socket|requests|urllib|httpx|aiohttp|ftplib|smtplib|telnetlib)\b"), "Network access is blocked in the local code sandbox."),
    (re.compile(r"\b(?:eval|exec|compile|__import__|globals|locals|vars)\s*\("), "Dynamic code execution is blocked in the local code sandbox."),
    (re.compile(r"\b(?:ctypes|multiprocessing|threading|pty|resource)\b"), "Low-level process/system modules are blocked in the local code sandbox."),
    (re.compile(r"\b(?:open)\s*\(\s*[0-9]+"), "Opening raw file descriptors is blocked in the local code sandbox."),
)


_FS_PRELUDE = r'''
import builtins as _cosmic_builtins
import io as _cosmic_io
import os as _cosmic_os
import pathlib as _cosmic_pathlib
import shutil as _cosmic_shutil
import socket as _cosmic_socket

_COSMIC_ROOT = _cosmic_pathlib.Path(_cosmic_os.environ["COSMIC_CODE_SANDBOX_ROOT"]).resolve()
_COSMIC_OUTPUTS = _COSMIC_ROOT / "outputs"
_COSMIC_OUTPUTS.mkdir(parents=True, exist_ok=True)

def _cosmic_resolve_path(value):
    if isinstance(value, int):
        raise PermissionError("Raw file descriptors are blocked in the COSMIC code sandbox.")
    path = _cosmic_pathlib.Path(value)
    if not path.is_absolute():
        path = _cosmic_pathlib.Path.cwd() / path
    resolved = path.resolve()
    try:
        resolved.relative_to(_COSMIC_ROOT)
    except ValueError:
        raise PermissionError(f"Path escapes COSMIC code sandbox: {value}")
    return resolved

_cosmic_orig_open = _cosmic_builtins.open
_cosmic_orig_io_open = _cosmic_io.open

def _cosmic_guarded_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    resolved = _cosmic_resolve_path(file)
    return _cosmic_orig_open(resolved, mode, buffering, encoding, errors, newline, closefd, opener)

_cosmic_builtins.open = _cosmic_guarded_open
_cosmic_io.open = _cosmic_guarded_open

def _cosmic_guard_unary(fn):
    def wrapped(path, *args, **kwargs):
        resolved = _cosmic_resolve_path(path)
        return fn(resolved, *args, **kwargs)
    return wrapped

def _cosmic_guard_binary(fn):
    def wrapped(src, dst, *args, **kwargs):
        resolved_src = _cosmic_resolve_path(src)
        resolved_dst = _cosmic_resolve_path(dst)
        return fn(resolved_src, resolved_dst, *args, **kwargs)
    return wrapped

for _cosmic_name in ("remove", "unlink", "rmdir", "chdir", "mkdir", "makedirs"):
    if hasattr(_cosmic_os, _cosmic_name):
        setattr(_cosmic_os, _cosmic_name, _cosmic_guard_unary(getattr(_cosmic_os, _cosmic_name)))
for _cosmic_name in ("rename", "replace"):
    if hasattr(_cosmic_os, _cosmic_name):
        setattr(_cosmic_os, _cosmic_name, _cosmic_guard_binary(getattr(_cosmic_os, _cosmic_name)))
for _cosmic_name in ("copy", "copy2", "copyfile", "move", "copytree"):
    if hasattr(_cosmic_shutil, _cosmic_name):
        setattr(_cosmic_shutil, _cosmic_name, _cosmic_guard_binary(getattr(_cosmic_shutil, _cosmic_name)))
if hasattr(_cosmic_shutil, "rmtree"):
    _cosmic_shutil.rmtree = _cosmic_guard_unary(_cosmic_shutil.rmtree)

if _cosmic_os.environ.get("COSMIC_CODE_SANDBOX_ALLOW_NETWORK", "").strip().lower() not in {"1", "true", "yes", "on"}:
    class _CosmicNetworkBlocked:
        def __init__(self, *args, **kwargs):
            raise PermissionError("Network access is blocked in the COSMIC code sandbox.")

    def _cosmic_network_blocked(*args, **kwargs):
        raise PermissionError("Network access is blocked in the COSMIC code sandbox.")

    _cosmic_socket.socket = _CosmicNetworkBlocked
    _cosmic_socket.create_connection = _cosmic_network_blocked
    _cosmic_socket.getaddrinfo = _cosmic_network_blocked
    _cosmic_socket.gethostbyname = _cosmic_network_blocked

try:
    import resource as _cosmic_resource
    _cosmic_resource.setrlimit(_cosmic_resource.RLIMIT_NOFILE, (64, 64))
    _cosmic_resource.setrlimit(_cosmic_resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
except Exception:
    pass

'''


_HOST_GRANT_FS_EXTENSION = '''
_COSMIC_HOST_READ = {host_read_paths!r}
_COSMIC_HOST_WRITE = {host_write_paths!r}

def _cosmic_path_in_granted_tree(resolved, granted_paths):
    for granted in granted_paths:
        base = _cosmic_pathlib.Path(granted).resolve()
        try:
            resolved.relative_to(base)
            return True
        except ValueError:
            continue
    return False

def _cosmic_resolve_path(value):
    if isinstance(value, int):
        raise PermissionError("Raw file descriptors are blocked in the COSMIC code sandbox.")
    path = _cosmic_pathlib.Path(value)
    if not path.is_absolute():
        path = _cosmic_pathlib.Path.cwd() / path
    resolved = path.resolve()
    try:
        resolved.relative_to(_COSMIC_ROOT)
        return resolved
    except ValueError:
        pass
    if _cosmic_path_in_granted_tree(resolved, _COSMIC_HOST_READ + _COSMIC_HOST_WRITE):
        return resolved
    raise PermissionError(f"Path escapes COSMIC code sandbox: {{value}}")

def _cosmic_mode_allows_write(mode):
    mode = str(mode or "r")
    return any(flag in mode for flag in ("w", "a", "+", "x"))

def _cosmic_guarded_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
    resolved = _cosmic_resolve_path(file)
    if _cosmic_mode_allows_write(mode):
        if not _cosmic_path_in_granted_tree(resolved, _COSMIC_HOST_WRITE):
            try:
                resolved.relative_to(_COSMIC_ROOT / "outputs")
            except ValueError:
                raise PermissionError(f"Write access is not granted for: {{file}}")
    elif not _cosmic_path_in_granted_tree(resolved, _COSMIC_HOST_READ + _COSMIC_HOST_WRITE):
        try:
            resolved.relative_to(_COSMIC_ROOT)
        except ValueError:
            raise PermissionError(f"Read access is not granted for: {{file}}")
    return _cosmic_orig_open(resolved, mode, buffering, encoding, errors, newline, closefd, opener)

_cosmic_builtins.open = _cosmic_guarded_open
_cosmic_io.open = _cosmic_guarded_open

# User code may now `import os` directly (host filesystem access was granted and
# approved). Keep the sandbox invariants intact: block process spawning entirely
# and scope low-level filesystem entry points to the sandbox root + granted trees.
def _cosmic_os_process_blocked(*args, **kwargs):
    raise PermissionError("Process execution is blocked in the COSMIC code sandbox.")

for _cosmic_name in (
    "system", "popen", "fork", "forkpty",
    "exec", "execl", "execle", "execlp", "execlpe",
    "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe",
    "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp", "startfile",
):
    if hasattr(_cosmic_os, _cosmic_name):
        setattr(_cosmic_os, _cosmic_name, _cosmic_os_process_blocked)

_cosmic_orig_os_open = _cosmic_os.open
def _cosmic_guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
    resolved = _cosmic_resolve_path(path)
    return _cosmic_orig_os_open(resolved, flags, mode, dir_fd=dir_fd)
_cosmic_os.open = _cosmic_guarded_os_open

_cosmic_orig_scandir = _cosmic_os.scandir
def _cosmic_guarded_scandir(path="."):
    resolved = _cosmic_resolve_path(path)
    return _cosmic_orig_scandir(resolved)
_cosmic_os.scandir = _cosmic_guarded_scandir

_cosmic_orig_listdir = _cosmic_os.listdir
def _cosmic_guarded_listdir(path="."):
    resolved = _cosmic_resolve_path(path)
    return _cosmic_orig_listdir(resolved)
_cosmic_os.listdir = _cosmic_guarded_listdir
'''

_NETWORK_HOST_ALLOWLIST_EXTENSION = '''
import json as _cosmic_json
_COSMIC_ALLOWED_HOSTS = _cosmic_json.loads(_cosmic_os.environ.get("COSMIC_CODE_SANDBOX_ALLOWED_HOSTS", "[]") or "[]")

def _cosmic_normalize_host_label(host):
    text = str(host or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if ":" in text and not text.startswith("http"):
        text = text.rsplit(":", 1)[0]
    return text

def _cosmic_host_is_allowed(host):
    normalized = _cosmic_normalize_host_label(host)
    if not normalized:
        return False
    if not _COSMIC_ALLOWED_HOSTS:
        return True
    for allowed in _COSMIC_ALLOWED_HOSTS:
        allowed_norm = _cosmic_normalize_host_label(allowed)
        if not allowed_norm:
            continue
        if normalized == allowed_norm or normalized.endswith("." + allowed_norm):
            return True
    return False

_cosmic_orig_create_connection = _cosmic_socket.create_connection
def _cosmic_filtered_create_connection(address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _cosmic_host_is_allowed(host):
        raise PermissionError(f"Network access to host is not allowed: {{host}}")
    return _cosmic_orig_create_connection(address, *args, **kwargs)
_cosmic_socket.create_connection = _cosmic_filtered_create_connection

_cosmic_orig_getaddrinfo = _cosmic_socket.getaddrinfo
def _cosmic_filtered_getaddrinfo(host, port, *args, **kwargs):
    if host and not _cosmic_host_is_allowed(host):
        raise PermissionError(f"Network access to host is not allowed: {{host}}")
    return _cosmic_orig_getaddrinfo(host, port, *args, **kwargs)
_cosmic_socket.getaddrinfo = _cosmic_filtered_getaddrinfo
'''

_FONT_FALLBACK_PRELUDE = '''
try:
    import matplotlib as _cosmic_mpl
    _cosmic_mpl.rcParams["font.family"] = "DejaVu Sans"
    _cosmic_mpl.rcParams["font.sans-serif"] = [
        "DejaVu Sans",
        "Liberation Sans",
        "Arial",
        "Helvetica",
        "sans-serif",
    ]
except Exception:
    pass
'''


@dataclass(slots=True)
class LocalCodeSandboxSettings:
    enabled: bool = True
    timeout_sec: float = 45.0
    allow_network: bool = False
    allow_pip: bool = True
    pip_timeout_sec: float = 120.0
    venv_cache_root: Path | None = None
    max_script_bytes: int = 256000
    max_files: int = 12
    max_file_bytes: int = 25 * 1024 * 1024
    host_read_paths: tuple[str, ...] = ()
    host_write_paths: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()


def run_local_code_sandbox(
    *,
    code: str,
    artifacts_root: Path,
    task_id: str,
    description: str = "",
    packages: list[str] | None = None,
    timeout_sec: float | None = None,
    settings: LocalCodeSandboxSettings | None = None,
) -> dict[str, Any]:
    settings = settings or LocalCodeSandboxSettings()
    if not settings.enabled:
        return {"error": True, "tool": "cosmic_code_execution", "message": "Local code execution sandbox is disabled."}

    code = str(code or "")
    if not code.strip():
        return {"error": True, "tool": "cosmic_code_execution", "message": "code is required."}
    encoded = code.encode("utf-8")
    if len(encoded) > settings.max_script_bytes:
        return {
            "error": True,
            "tool": "cosmic_code_execution",
            "message": f"code is too large for one sandbox run ({len(encoded)} bytes > {settings.max_script_bytes}).",
        }
    allow_host_fs = bool(settings.host_read_paths or settings.host_write_paths)
    validation_error = _validate_code(
        code,
        allow_network=settings.allow_network,
        allow_host_fs=allow_host_fs,
    )
    if validation_error:
        return {"error": True, "tool": "cosmic_code_execution", "message": validation_error}

    normalized_packages = _normalize_packages(packages or [])
    if normalized_packages and not settings.allow_pip:
        return {
            "error": True,
            "tool": "cosmic_code_execution",
            "message": "Package installation is disabled for this local code sandbox. Use installed packages or delegate larger setup to Alpha.",
        }

    artifacts_root = artifacts_root.expanduser().resolve()
    execution_id = f"code_{uuid4().hex[:12]}"
    safe_task_id = _safe_component(task_id or "task")
    root = artifacts_root / safe_task_id / "orchestrator" / "local_code_execution" / execution_id
    code_dir = root / "code"
    outputs_dir = root / "outputs"
    receipt_dir = root / "executions"
    home_dir = root / ".sandbox_home"
    for directory in (code_dir, outputs_dir, receipt_dir, home_dir):
        directory.mkdir(parents=True, exist_ok=True)

    script_path = code_dir / "main.py"
    prelude = _compose_prelude(
        host_read_paths=list(settings.host_read_paths),
        host_write_paths=list(settings.host_write_paths),
        allow_network=settings.allow_network,
        allowed_hosts=list(settings.allowed_hosts),
    )
    script_path.write_text(f"{prelude}\n# User code starts here\n{code}\n", encoding="utf-8")

    python_exe = sys.executable
    pip_log = ""
    if normalized_packages:
        python_exe, pip_log = _provision_venv(
            packages=normalized_packages,
            settings=settings,
            root=root,
        )

    env = _build_env(
        root=root,
        home_dir=home_dir,
        allow_network=settings.allow_network,
        allowed_hosts=list(settings.allowed_hosts),
    )
    effective_timeout = _coerce_timeout(timeout_sec, settings.timeout_sec)
    started = time.perf_counter()
    timed_out = False
    exit_code = 0
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            [python_exe, "-I", str(script_path)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
        exit_code = int(completed.returncode)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = _bytes_or_text_to_str(exc.stdout)
        stderr = _bytes_or_text_to_str(exc.stderr)
        stderr = (stderr + "\n" if stderr else "") + f"Timed out after {effective_timeout:.1f}s."
    duration_ms = int((time.perf_counter() - started) * 1000)

    artifacts, skipped = _collect_output_artifacts(
        artifacts_root=artifacts_root,
        root=root,
        task_id=safe_task_id,
        execution_id=execution_id,
        max_files=max(0, settings.max_files),
        max_file_bytes=max(1024, settings.max_file_bytes),
    )
    receipt = {
        "tool": "cosmic_code_execution",
        "execution_id": execution_id,
        "task_id": safe_task_id,
        "description": description,
        "status": "timeout" if timed_out else ("completed" if exit_code == 0 else "failed"),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "code_sha256": hashlib.sha256(encoded).hexdigest(),
        "packages": normalized_packages,
        "pip_log": _truncate(pip_log, 12000),
        "artifacts": artifacts,
        "skipped_artifacts": skipped,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = receipt_dir / f"{execution_id}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    stdout_text, stdout_truncated = _truncate_with_flag(stdout)
    stderr_text, stderr_truncated = _truncate_with_flag(stderr)
    return {
        "tool": "cosmic_code_execution",
        "execution_id": execution_id,
        "status": receipt["status"],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
        "skipped_artifacts": skipped,
        "receipt_path": _logical_path(artifacts_root, receipt_path),
        "working_directory": str(root),
        "message": _result_message(exit_code=exit_code, timed_out=timed_out, artifact_count=len(artifacts)),
    }


def _validate_code(code: str, *, allow_network: bool, allow_host_fs: bool) -> str | None:
    patterns: list[tuple[re.Pattern[str], str]] = []
    for pattern, message in _DENY_PATTERNS:
        if allow_network and "Network access" in message:
            continue
        # When host filesystem access has been explicitly approved, the user code
        # needs `os`/`sys` (os.walk, os.listdir, os.path, sys.path) to do anything
        # useful. The runtime prelude keeps reads/writes scoped to granted trees
        # and blocks process spawning, so the static block is no longer required.
        if allow_host_fs and ("Direct os imports" in message or "Direct sys imports" in message):
            continue
        patterns.append((pattern, message))
    for pattern, message in patterns:
        if pattern.search(code):
            return message
    return None


def _normalize_packages(packages: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in packages:
        package = str(item or "").strip()
        if not package or package in seen:
            continue
        if not _PACKAGE_RE.match(package):
            raise ValueError(f"Unsupported package specifier: {package}")
        seen.add(package)
        normalized.append(package)
    return normalized[:12]


def _provision_venv(
    *,
    packages: list[str],
    settings: LocalCodeSandboxSettings,
    root: Path,
) -> tuple[str, str]:
    cache_root = (settings.venv_cache_root or (root / ".venv_cache")).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256("\n".join(sorted(packages)).encode("utf-8")).hexdigest()[:16]
    venv_dir = cache_root / f"py-{sys.version_info.major}{sys.version_info.minor}-{digest}"
    log_parts: list[str] = []
    if not venv_dir.exists():
        venv.EnvBuilder(with_pip=True, clear=False).create(venv_dir)
    python_exe = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pip_exe = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    completed = subprocess.run(
        [str(pip_exe), "install", "--disable-pip-version-check", *packages],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=max(10.0, settings.pip_timeout_sec),
    )
    log_parts.append(completed.stdout or "")
    log_parts.append(completed.stderr or "")
    if completed.returncode != 0:
        raise RuntimeError(f"Package installation failed: {_truncate(''.join(log_parts), 2000)}")
    return str(python_exe), "".join(log_parts)


def _compose_prelude(
    *,
    host_read_paths: list[str],
    host_write_paths: list[str],
    allow_network: bool,
    allowed_hosts: list[str],
) -> str:
    prelude = _compose_fs_prelude(
        host_read_paths=host_read_paths,
        host_write_paths=host_write_paths,
    )
    if allow_network and allowed_hosts:
        prelude += _NETWORK_HOST_ALLOWLIST_EXTENSION
    prelude += _FONT_FALLBACK_PRELUDE
    return prelude


def _compose_fs_prelude(*, host_read_paths: list[str], host_write_paths: list[str]) -> str:
    if host_read_paths or host_write_paths:
        return _FS_PRELUDE + _HOST_GRANT_FS_EXTENSION.format(
            host_read_paths=host_read_paths,
            host_write_paths=host_write_paths,
        )
    return _FS_PRELUDE


def _build_env(
    *,
    root: Path,
    home_dir: Path,
    allow_network: bool,
    allowed_hosts: list[str] | None = None,
) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "COSMIC_CODE_SANDBOX_ROOT": str(root),
        "COSMIC_CODE_SANDBOX_ALLOW_NETWORK": "true" if allow_network else "false",
        "COSMIC_CODE_SANDBOX_ALLOWED_HOSTS": json.dumps(allowed_hosts or []),
        "HOME": str(home_dir),
        "USERPROFILE": str(home_dir),
        "TMPDIR": str(root / "tmp"),
        "TEMP": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "MPLCONFIGDIR": str(root / ".matplotlib"),
    }
    for key in ("SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["MPLCONFIGDIR"]).mkdir(parents=True, exist_ok=True)
    return env


def _collect_output_artifacts(
    *,
    artifacts_root: Path,
    root: Path,
    task_id: str,
    execution_id: str,
    max_files: int,
    max_file_bytes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    artifacts: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    excluded_dirs = {"code", "executions", ".sandbox_home", ".venv_cache", "__pycache__", "tmp", ".matplotlib"}
    candidates = sorted(path for path in root.rglob("*") if path.is_file())
    for path in candidates:
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in excluded_dirs:
            continue
        if len(artifacts) >= max_files:
            skipped.append({"path": str(rel), "reason": "max_files"})
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped.append({"path": str(rel), "reason": "stat_failed"})
            continue
        if size > max_file_bytes:
            skipped.append({"path": str(rel), "reason": "max_file_bytes", "size_bytes": size})
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        filename = path.name
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        artifact_suffix = hashlib.sha256(f"{execution_id}:{rel.as_posix()}:{digest}".encode("utf-8")).hexdigest()[:12]
        artifacts.append(
            {
                "artifact_id": f"art_code_{execution_id}_{artifact_suffix}",
                "task_id": task_id,
                "mime": mime,
                "mime_type": mime,
                "path": _logical_path(artifacts_root, path),
                "kind": "local_code_output",
                "audience": "deliverable",
                "filename": filename,
                "size_bytes": size,
                "created_by_agent": "cosmic/orchestrator:1.0.0",
                "sha256": digest,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return artifacts, skipped[:20]


def _logical_path(artifacts_root: Path, path: Path) -> str:
    resolved_root = artifacts_root.resolve()
    resolved_path = path.resolve()
    try:
        rel = resolved_path.relative_to(resolved_root)
        return f"runs/artifacts/{rel.as_posix()}"
    except ValueError:
        return str(resolved_path)


def _safe_component(value: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("_", str(value or "").strip()).strip("._")
    return cleaned[:80] or "task"


def _truncate(value: str, limit: int = _MAX_CAPTURE_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def _truncate_with_flag(value: str, limit: int = _MAX_CAPTURE_CHARS) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return _truncate(text, limit), True


def _bytes_or_text_to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _coerce_timeout(value: float | None, default: float) -> float:
    try:
        requested = float(value) if value is not None else float(default)
    except (TypeError, ValueError):
        requested = float(default)
    return max(1.0, min(requested, max(1.0, float(default))))


def _result_message(*, exit_code: int, timed_out: bool, artifact_count: int) -> str:
    if timed_out:
        return "Local code sandbox timed out before completion."
    if exit_code != 0:
        return "Local code sandbox finished with an error."
    if artifact_count:
        return f"Local code sandbox completed and produced {artifact_count} file(s)."
    return "Local code sandbox completed."
