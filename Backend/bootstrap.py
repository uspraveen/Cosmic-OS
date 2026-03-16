#!/usr/bin/env python3
"""
Bootstrap helper for COSMIC Backend VM setup.

This script is meant to be the first thing run on a Linux VM after cloning
the backend repo. It currently handles Python readiness, pip availability,
virtual environment creation, backend dependency installation, and WhatsApp
bridge dependency setup. It can also invoke the dedicated VM edge setup
script for Caddy/TLS when a public hostname is configured. It is intentionally
structured so future setup steps
can be added without turning it into an unmaintainable script.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import getpass
import secrets
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MIN_PYTHON = (3, 10)
BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_VENV_PATH = BACKEND_ROOT / ".venv"
DEFAULT_REQUIREMENTS_PATH = BACKEND_ROOT / "requirements.txt"
DEFAULT_BRIDGE_DIR = BACKEND_ROOT / "bridges" / "whatsapp_bridge"
DEFAULT_SYSTEMD_TEMPLATE_DIR = BACKEND_ROOT / "systemd"
DEFAULT_EDGE_SETUP_SCRIPT = BACKEND_ROOT / "vm_edge_setup.py"
DEFAULT_GATEWAY_ENV_PATH = BACKEND_ROOT / "gateway.env"
DEFAULT_ORCHESTRATOR_ENV_PATH = BACKEND_ROOT / "orchestrator.env"
DEFAULT_MEMORY_ENV_PATH = BACKEND_ROOT / "memory.env"
DEFAULT_ENV_SEARCH_ROOTS = (
    BACKEND_ROOT,
    BACKEND_ROOT / "bridges",
)
DEFAULT_SYSTEM_ENV_DIR = Path("/etc/cosmic")
DEFAULT_WHATSAPP_AUTH_DIR = Path("/var/lib/cosmic/whatsapp/auth")
DEFAULT_MEMORY_DATA_DIR = Path("/var/lib/cosmic/memory")
DEFAULT_SUPABASE_URL = "https://hluenippcdiejenmteen.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhsdWVuaXBwY2RpZWplbm10ZWVuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTE4MzYwOTMsImV4cCI6MjA2NzQxMjA5M30."
    "dm6YO4B9SAQ8hnGtR-OZS7jn5FcL-zz4s4XxP-TyCpk"
)
DEFAULT_SUPABASE_BOOTSTRAP_RPC = "consume_bootstrap_token"
REQUIRED_SERVICE_ENV_KEYS: Dict[str, Tuple[str, ...]] = {
    "model-router.env": ("GROQ_API_KEY",),
}
OPTIONAL_SYSTEMD_TEMPLATES = frozenset({"cosmic-memory.service.example"})
PYTHON_CANDIDATES = [
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
]
MIN_NODE_MAJOR = 20
DEFAULT_RETRY_ATTEMPTS = 4
DEFAULT_RETRY_INITIAL_DELAY_SEC = 1.5
PACKAGE_NAMES: Dict[str, Dict[str, str]] = {
    "python": {
        "apt-get": "python3",
        "dnf": "python3",
        "yum": "python3",
        "apk": "python3",
    },
    "pip": {
        "apt-get": "python3-pip",
        "dnf": "python3-pip",
        "yum": "python3-pip",
        "apk": "py3-pip",
    },
    "venv": {
        "apt-get": "python3-venv",
        "dnf": "python3",
        "yum": "python3",
        "apk": "py3-virtualenv",
    },
    "nodejs": {
        "apt-get": "nodejs",
        "dnf": "nodejs",
        "yum": "nodejs",
        "apk": "nodejs",
    },
    "npm": {
        "apt-get": "npm",
        "dnf": "npm",
        "yum": "npm",
        "apk": "npm",
    },
    "redis": {
        "apt-get": "redis-server",
        "dnf": "redis",
        "yum": "redis",
        "apk": "redis",
    },
}


class BootstrapError(RuntimeError):
    pass


RetryResult = TypeVar("RetryResult")


def log(message: str) -> None:
    print("[bootstrap] {0}".format(message))


def command_str(command: Sequence[str]) -> str:
    return shlex.join(list(command))


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid and geteuid() == 0)


def run(
    command: Sequence[str],
    *,
    use_sudo: bool = False,
    capture_output: bool = False,
    check: bool = True,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    full_command = list(command)
    if use_sudo and not is_root():
        sudo_path = shutil.which("sudo")
        if not sudo_path:
            raise BootstrapError(
                "System package install requires root or sudo. Missing sudo for command: {0}".format(
                    command_str(command)
                )
            )
        full_command = [sudo_path] + full_command

    log("Running: {0}".format(command_str(full_command)))
    return subprocess.run(
        full_command,
        check=check,
        text=True,
        capture_output=capture_output,
        cwd=str(cwd) if cwd else None,
    )


def retry_call(
    operation_label: str,
    action: Callable[[], RetryResult],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    initial_delay_sec: float = DEFAULT_RETRY_INITIAL_DELAY_SEC,
    max_delay_sec: float = 15.0,
    retry_exceptions: Tuple[type[BaseException], ...],
    should_retry: Optional[Callable[[BaseException], bool]] = None,
) -> RetryResult:
    normalized_attempts = max(1, attempts)
    delay_sec = max(0.0, initial_delay_sec)
    for attempt in range(1, normalized_attempts + 1):
        try:
            return action()
        except retry_exceptions as exc:
            if should_retry is not None and not should_retry(exc):
                raise
            if attempt >= normalized_attempts:
                raise
            log(
                "{0} failed on attempt {1}/{2}: {3}. Retrying in {4:.1f}s.".format(
                    operation_label,
                    attempt,
                    normalized_attempts,
                    exc,
                    delay_sec,
                )
            )
            time.sleep(delay_sec)
            delay_sec = min(max_delay_sec, max(delay_sec * 2, 0.5))

    raise AssertionError("retry_call exhausted without returning or raising")


def run_with_retry(
    command: Sequence[str],
    *,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    initial_delay_sec: float = DEFAULT_RETRY_INITIAL_DELAY_SEC,
    use_sudo: bool = False,
    capture_output: bool = False,
    check: bool = True,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    return retry_call(
        "Command failed: {0}".format(command_str(command)),
        lambda: run(
            command,
            use_sudo=use_sudo,
            capture_output=capture_output,
            check=check,
            cwd=cwd,
        ),
        attempts=attempts,
        initial_delay_sec=initial_delay_sec,
        retry_exceptions=(subprocess.CalledProcessError,),
    )


def should_retry_bootstrap_http_error(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code >= 500 or exc.code in (408, 425, 429)
    if isinstance(exc, URLError):
        return True
    return False


def detect_package_manager() -> Optional[str]:
    for manager in ("apt-get", "dnf", "yum", "apk"):
        if shutil.which(manager):
            return manager
    return None


def executable_version(command: Sequence[str]) -> Optional[str]:
    try:
        result = run(command, capture_output=True)
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return None
    return (result.stdout or "").strip() or None


def node_major_version(version_text: Optional[str]) -> Optional[int]:
    if not version_text:
        return None
    normalized = version_text.strip()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    major, *_rest = normalized.split(".", 1)
    try:
        return int(major)
    except ValueError:
        return None


def install_system_packages(manager: str, packages: Iterable[str]) -> None:
    package_list = [pkg for pkg in packages if pkg]
    if not package_list:
        return

    if manager == "apt-get":
        run_with_retry(["apt-get", "update"], use_sudo=True)
        run_with_retry(["apt-get", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "dnf":
        run_with_retry(["dnf", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "yum":
        run_with_retry(["yum", "install", "-y", *package_list], use_sudo=True)
        return
    if manager == "apk":
        run_with_retry(["apk", "add", "--no-cache", *package_list], use_sudo=True)
        return

    raise BootstrapError("Unsupported package manager: {0}".format(manager))


def example_target_path(example_path: Path) -> Path:
    if example_path.name == ".env.example":
        return example_path.with_name(".env")
    if example_path.name.endswith(".env.example"):
        return example_path.with_name(example_path.name[: -len(".example")])
    raise BootstrapError("Unsupported env example naming: {0}".format(example_path))


def discover_env_example_files(search_roots: Sequence[Path]) -> List[Path]:
    discovered: list[Path] = []
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name != ".env.example" and not path.name.endswith(".env.example"):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            discovered.append(resolved)
    return discovered


def ensure_env_files(search_roots: Sequence[Path]) -> List[Path]:
    created: list[Path] = []
    for example_path in discover_env_example_files(search_roots):
        target_path = example_target_path(example_path)
        if target_path.exists():
            log("Environment file already exists: {0}".format(target_path))
            continue

        shutil.copy2(example_path, target_path)
        created.append(target_path)
        log("Created env file from template: {0}".format(target_path))
    return created


def read_text_file(path: Path, *, use_sudo: bool = False) -> str:
    if use_sudo and not is_root():
        result = run(["cat", str(path)], use_sudo=True, capture_output=True)
        return result.stdout
    return path.read_text(encoding="utf-8")


def install_text_file(path: Path, content: str, *, mode: str = "600", use_sudo: bool = False) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", newline="\n") as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    try:
        run(["install", "-m", mode, str(temp_path), str(path)], use_sudo=use_sudo)
    finally:
        temp_path.unlink(missing_ok=True)


def trim_blank_lines(lines: Sequence[str]) -> List[str]:
    trimmed = list(lines)
    while trimmed and not trimmed[0].strip():
        trimmed.pop(0)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def replace_placeholder_env_entries(existing_raw: str, source_raw: str) -> Tuple[str, List[str]]:
    source_values = parse_env_text(source_raw)
    replaced_keys: list[str] = []
    rendered_lines: list[str] = []

    for line in existing_raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered_lines.append(line)
            continue

        key, value = line.split("=", 1)
        env_key = key.strip()
        source_value = meaningful_env_value(source_values.get(env_key))
        existing_value = meaningful_env_value(value.strip())
        if source_value is not None and existing_value is None:
            rendered_lines.append("{0}={1}".format(key, source_value))
            replaced_keys.append(env_key)
        else:
            rendered_lines.append(line)

    merged = "\n".join(rendered_lines).rstrip()
    if merged:
        merged += "\n"
    return merged, replaced_keys


def merge_missing_env_entries(existing_raw: str, source_raw: str) -> Tuple[str, List[str]]:
    existing_reconciled, replaced_keys = replace_placeholder_env_entries(existing_raw, source_raw)
    existing_keys = set(parse_env_text(existing_reconciled))
    pending_block: list[str] = []
    missing_blocks: list[Tuple[str, List[str]]] = []

    for line in source_raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if pending_block and pending_block[-1] != "":
                pending_block.append("")
            continue

        if stripped.startswith("#") or "=" not in line:
            pending_block.append(line)
            continue

        key, _value = line.split("=", 1)
        env_key = key.strip()
        if env_key in existing_keys:
            pending_block = []
            continue

        block = trim_blank_lines(pending_block)
        block.append(line)
        missing_blocks.append((env_key, block))
        existing_keys.add(env_key)
        pending_block = []

    if not missing_blocks:
        normalized = (
            existing_reconciled
            if existing_reconciled.endswith("\n") or not existing_reconciled
            else existing_reconciled + "\n"
        )
        return normalized, replaced_keys

    existing_body = existing_reconciled.rstrip("\n")
    appended_sections = ["\n".join(block).rstrip() for _key, block in missing_blocks if block]
    appended_body = "\n\n".join(section for section in appended_sections if section).strip()
    if existing_body and appended_body:
        merged = existing_body + "\n\n" + appended_body + "\n"
    elif appended_body:
        merged = appended_body + "\n"
    else:
        merged = existing_body + ("\n" if existing_body else "")
    return merged, replaced_keys + [key for key, _block in missing_blocks]


def sync_env_file(
    target_path: Path,
    *,
    source_raw: str,
    create_missing: bool = True,
    use_sudo: bool = False,
    mode: str = "600",
) -> List[str]:
    if not target_path.exists():
        if not create_missing:
            log("Skipping missing env file during sync: {0}".format(target_path))
            return []
        if use_sudo:
            install_text_file(target_path, source_raw, mode=mode, use_sudo=True)
        else:
            target_path.write_text(source_raw, encoding="utf-8")
        log("Created env file during sync: {0}".format(target_path))
        return list(parse_env_text(source_raw))

    existing_raw = read_text_file(target_path, use_sudo=use_sudo)
    merged, added_keys = merge_missing_env_entries(existing_raw, source_raw)
    if not added_keys:
        log("Env file already has all template keys: {0}".format(target_path))
        return []
    if use_sudo:
        install_text_file(target_path, merged, mode=mode, use_sudo=True)
    else:
        target_path.write_text(merged, encoding="utf-8")
    log(
        "Updated env file {0}: {1}".format(
            target_path,
            ", ".join(added_keys),
        )
    )
    return added_keys


def parse_env_text(raw: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def render_env_with_overrides(raw: str, overrides: Dict[str, str]) -> str:
    rendered_lines: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered_lines.append(line)
            continue

        key, _value = line.split("=", 1)
        env_key = key.strip()
        if env_key in overrides:
            rendered_lines.append("{0}={1}".format(key, overrides[env_key]))
            seen.add(env_key)
        else:
            rendered_lines.append(line)

    for key, value in overrides.items():
        if key not in seen:
            rendered_lines.append("{0}={1}".format(key, value))

    return "\n".join(rendered_lines).rstrip() + "\n"


def meaningful_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith("<") and normalized.endswith(">"):
        return None
    return normalized


def extract_host_from_url(value: Optional[str]) -> Optional[str]:
    normalized = meaningful_env_value(value)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    if parsed.netloc:
        return parsed.netloc
    if parsed.path:
        host = parsed.path.split("/", 1)[0]
        return host or None
    return None


def is_local_redis_url(value: Optional[str]) -> bool:
    normalized = meaningful_env_value(value)
    if normalized is None:
        return False
    parsed = urlparse(normalized)
    if parsed.scheme not in {"redis", "rediss"}:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def resolve_configured_redis_url() -> Optional[str]:
    candidates = [
        BACKEND_ROOT / "gateway.env",
        BACKEND_ROOT / "gateway.env.example",
        BACKEND_ROOT / "orchestrator.env",
        BACKEND_ROOT / "orchestrator.env.example",
    ]
    for path in candidates:
        if not path.exists():
            continue
        parsed = parse_env_text(path.read_text(encoding="utf-8"))
        redis_url = meaningful_env_value(parsed.get("REDIS_URL"))
        if redis_url is not None:
            return redis_url
    return None


def setup_local_redis(redis_url: Optional[str]) -> None:
    if not is_local_redis_url(redis_url):
        return
    if not is_linux():
        raise BootstrapError("Local Redis provisioning currently targets Linux VMs only.")
    manager = detect_package_manager()
    if not manager:
        raise BootstrapError("REDIS_URL points to localhost, but no supported package manager was found.")
    package_name = PACKAGE_NAMES["redis"].get(manager)
    if not package_name:
        raise BootstrapError("No Redis package mapping for package manager: {0}".format(manager))

    log("Ensuring local Redis is installed for task input queues via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    if shutil.which("systemctl") is None:
        log("systemctl not found; skipping Redis service enable/start.")
        return

    service_candidates = {
        "apt-get": ("redis-server",),
        "dnf": ("redis",),
        "yum": ("redis",),
        "apk": ("redis",),
    }.get(manager, ())
    for service_name in service_candidates:
        try:
            run(["systemctl", "enable", service_name], use_sudo=True)
            run(["systemctl", "restart", service_name], use_sudo=True)
            log("Local Redis service is active: {0}".format(service_name))
            return
        except (BootstrapError, subprocess.CalledProcessError):
            continue

    raise BootstrapError(
        "Installed Redis package, but could not enable a known Redis service for manager {0}.".format(manager)
    )


def missing_required_env_keys(env_path: Path, required_keys: Sequence[str]) -> List[str]:
    if not required_keys:
        return []
    if not env_path.exists():
        return list(required_keys)

    parsed = parse_env_text(env_path.read_text(encoding="utf-8"))
    missing: list[str] = []
    for key in required_keys:
        if meaningful_env_value(parsed.get(key)) is None:
            missing.append(key)
    return missing


def validate_required_service_env_files(effective_sources: Sequence[Tuple[Path, Path]]) -> None:
    failures: list[str] = []
    for source_path, dest_path in effective_sources:
        required_keys = REQUIRED_SERVICE_ENV_KEYS.get(dest_path.name, ())
        if not required_keys:
            continue

        missing_keys = missing_required_env_keys(source_path, required_keys)
        if missing_keys:
            failures.append(
                "{0}: missing required values for {1}".format(
                    source_path,
                    ", ".join(missing_keys),
                )
            )

    if failures:
        raise BootstrapError(
            "Required service env values are missing or placeholders remain.\n"
            "Fill the local env files before provisioning services:\n  - {0}".format(
                "\n  - ".join(failures)
            )
        )


def normalize_bootstrap_env_payload(payload: Dict[str, object]) -> Dict[str, Dict[str, str]]:
    if not isinstance(payload, dict):
        raise BootstrapError("Supabase bootstrap RPC returned an unexpected payload shape.")

    if payload.get("success") is False:
        message = payload.get("message") or payload.get("error") or "Unknown bootstrap error."
        raise BootstrapError("Supabase bootstrap RPC failed: {0}".format(message))

    gateway_env = dict(payload.get("gateway_env") or {}) if isinstance(payload.get("gateway_env"), dict) else {}
    orchestrator_env = (
        dict(payload.get("orchestrator_env") or {}) if isinstance(payload.get("orchestrator_env"), dict) else {}
    )
    model_router_env = (
        dict(payload.get("model_router_env") or {}) if isinstance(payload.get("model_router_env"), dict) else {}
    )
    meeting_env = dict(payload.get("meeting_env") or {}) if isinstance(payload.get("meeting_env"), dict) else {}
    vm_payload = dict(payload.get("vm") or {}) if isinstance(payload.get("vm"), dict) else {}

    if meaningful_env_value(model_router_env.get("GROQ_API_KEY")) is None:
        legacy_groq_api_key = meaningful_env_value(meeting_env.get("GROQ_API_KEY"))
        if legacy_groq_api_key is not None:
            model_router_env["GROQ_API_KEY"] = legacy_groq_api_key

    if meaningful_env_value(orchestrator_env.get("ANTHROPIC_MODEL")) is None:
        legacy_opus_model = meaningful_env_value(orchestrator_env.get("OPUS_MODEL"))
        if legacy_opus_model is not None:
            orchestrator_env["ANTHROPIC_MODEL"] = legacy_opus_model

    public_host = first_meaningful_value(
        gateway_env.get("GATEWAY_PUBLIC_HOST"),
        vm_payload.get("vm_dns") if isinstance(vm_payload.get("vm_dns"), str) else None,
        extract_host_from_url(vm_payload.get("gateway_url") if isinstance(vm_payload.get("gateway_url"), str) else None),
    )
    if public_host is not None:
        gateway_env["GATEWAY_PUBLIC_HOST"] = public_host

    normalized = {
        "gateway.env": gateway_env,
        "model-router.env": model_router_env,
        "orchestrator.env": orchestrator_env,
    }
    required_fields = {
        "gateway.env": ("GATEWAY_LOCAL_API_TOKEN", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "GATEWAY_PUBLIC_HOST"),
        "model-router.env": ("GROQ_API_KEY",),
        "orchestrator.env": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    }
    missing: list[str] = []
    for env_name, keys in required_fields.items():
        env_values = normalized[env_name]
        for key in keys:
            if meaningful_env_value(env_values.get(key)) is None:
                missing.append("{0}.{1}".format(env_name, key))
    if missing:
        raise BootstrapError(
            "Supabase bootstrap payload is missing required env values: {0}".format(", ".join(missing))
        )

    return normalized


def fetch_bootstrap_env_payload(
    *,
    bootstrap_token: str,
    supabase_url: str,
    supabase_anon_key: str,
) -> Dict[str, Dict[str, str]]:
    token = meaningful_env_value(bootstrap_token)
    supabase_base = meaningful_env_value(supabase_url)
    anon_key = meaningful_env_value(supabase_anon_key)
    if token is None:
        raise BootstrapError("A bootstrap token is required to fetch VM env values.")
    if supabase_base is None or anon_key is None:
        raise BootstrapError("Supabase URL and anon key are required to fetch VM env values.")

    rpc_url = "{0}/rest/v1/rpc/{1}".format(supabase_base.rstrip("/"), DEFAULT_SUPABASE_BOOTSTRAP_RPC)
    request = Request(
        rpc_url,
        data=json.dumps({"p_token": token}).encode("utf-8"),
        headers={
            "apikey": anon_key,
            "Authorization": "Bearer {0}".format(anon_key),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def perform_request() -> str:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")

    try:
        raw = retry_call(
            "Supabase bootstrap RPC request",
            perform_request,
            retry_exceptions=(HTTPError, URLError),
            should_retry=should_retry_bootstrap_http_error,
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BootstrapError(
            "Supabase bootstrap RPC returned HTTP {0}: {1}".format(exc.code, body or exc.reason)
        ) from exc
    except URLError as exc:
        raise BootstrapError("Failed to reach Supabase bootstrap RPC: {0}".format(exc.reason)) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError("Supabase bootstrap RPC returned invalid JSON.") from exc
    return normalize_bootstrap_env_payload(payload)


def version_for(executable: str) -> Optional[Tuple[int, int, int]]:
    try:
        result = run(
            [
                executable,
                "-c",
                "import sys; print('{0}.{1}.{2}'.format(*sys.version_info[:3]))",
            ],
            capture_output=True,
        )
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return None

    raw = (result.stdout or "").strip()
    if not raw:
        return None

    parts = raw.split(".")
    if len(parts) != 3:
        return None

    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def is_supported_python(version: Optional[Tuple[int, int, int]]) -> bool:
    return bool(version and version[:2] >= MIN_PYTHON)


def find_supported_python() -> Optional[str]:
    current = version_for(sys.executable)
    if is_supported_python(current):
        return sys.executable

    seen = set([sys.executable])
    for candidate in PYTHON_CANDIDATES:
        path = shutil.which(candidate)
        if not path or path in seen:
            continue
        seen.add(path)
        if is_supported_python(version_for(path)):
            return path
    return None


def maybe_reexec_with_supported_python() -> None:
    current_version = sys.version_info[:3]
    if current_version[:2] >= MIN_PYTHON:
        log(
            "Using Python {0}.{1}.{2} from {3}".format(
                current_version[0], current_version[1], current_version[2], sys.executable
            )
        )
        return

    supported = find_supported_python()
    if supported and Path(supported).resolve() != Path(sys.executable).resolve():
        log("Switching to supported interpreter: {0}".format(supported))
        os.execv(supported, [supported, str(Path(__file__).resolve()), *sys.argv[1:]])

    raise BootstrapError(
        "Python {0}.{1}.{2} is too old. COSMIC Backend needs Python {3}.{4}+.".format(
            current_version[0],
            current_version[1],
            current_version[2],
            MIN_PYTHON[0],
            MIN_PYTHON[1],
        )
    )


def ensure_python3_available() -> None:
    try:
        maybe_reexec_with_supported_python()
        return
    except BootstrapError:
        manager = detect_package_manager()
        if not is_linux() or not manager:
            raise

        package_name = PACKAGE_NAMES["python"].get(manager)
        if not package_name:
            raise

        log("Supported Python not found. Installing {0} via {1}.".format(package_name, manager))
        install_system_packages(manager, [package_name])

    supported = find_supported_python()
    if not supported:
        raise BootstrapError(
            "Installed python3 package, but no Python {0}.{1}+ interpreter was found. "
            "Use a newer distro/repository or install Python manually.".format(
                MIN_PYTHON[0], MIN_PYTHON[1]
            )
        )

    if Path(supported).resolve() != Path(sys.executable).resolve():
        log("Restarting bootstrap with {0}".format(supported))
        os.execv(supported, [supported, str(Path(__file__).resolve()), *sys.argv[1:]])


def has_pip() -> bool:
    try:
        run([sys.executable, "-m", "pip", "--version"], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError):
        return False


def ensure_pip() -> None:
    if has_pip():
        result = run([sys.executable, "-m", "pip", "--version"], capture_output=True)
        log("pip available: {0}".format((result.stdout or "").strip()))
        return

    log("pip not found. Trying ensurepip.")
    try:
        run([sys.executable, "-m", "ensurepip", "--upgrade"])
    except (BootstrapError, subprocess.CalledProcessError):
        manager = detect_package_manager()
        if not is_linux() or not manager:
            raise BootstrapError("Failed to install pip with ensurepip and no supported package manager was found.")

        package_name = PACKAGE_NAMES["pip"].get(manager)
        if not package_name:
            raise BootstrapError("No pip package mapping for package manager: {0}".format(manager))

        log("Installing pip package via {0}: {1}".format(manager, package_name))
        install_system_packages(manager, [package_name])

    if not has_pip():
        raise BootstrapError("pip is still unavailable after installation attempts.")


def has_venv_module() -> bool:
    try:
        run([sys.executable, "-m", "venv", "--help"], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError):
        return False


def can_create_virtualenv() -> bool:
    if not has_venv_module():
        return False

    with tempfile.TemporaryDirectory(prefix="cosmic-bootstrap-venv-check-") as temp_dir:
        try:
            run([sys.executable, "-m", "venv", temp_dir], capture_output=True)
            return True
        except (BootstrapError, subprocess.CalledProcessError):
            return False


def ensure_venv_support() -> None:
    if can_create_virtualenv():
        log("venv module available.")
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError("Python venv module is missing and no supported Linux package manager was found.")

    package_name = PACKAGE_NAMES["venv"].get(manager)
    if not package_name:
        raise BootstrapError("No venv package mapping for package manager: {0}".format(manager))

    log("Installing venv support via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    if not can_create_virtualenv():
        raise BootstrapError("Python venv support is still unavailable after installation attempts.")


def venv_python_path(venv_path: Path) -> Path:
    return venv_path / "bin" / "python"


def venv_has_pip(venv_path: Path) -> bool:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        return False
    try:
        run([str(python_path), "-m", "pip", "--version"], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError):
        return False


def ensure_virtualenv(venv_path: Path) -> None:
    if venv_python_path(venv_path).exists() and venv_has_pip(venv_path):
        log("Virtual environment already exists at {0}".format(venv_path))
        return

    if venv_path.exists():
        log("Removing incomplete virtual environment at {0}".format(venv_path))
        shutil.rmtree(venv_path)

    log("Creating virtual environment at {0}".format(venv_path))
    run([sys.executable, "-m", "venv", str(venv_path)])


def upgrade_venv_pip(venv_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError("Missing venv python executable at {0}".format(python_path))

    if not venv_has_pip(venv_path):
        log("pip missing inside virtual environment. Trying ensurepip.")
        run([str(python_path), "-m", "ensurepip", "--upgrade"])
        if not venv_has_pip(venv_path):
            raise BootstrapError("pip is still unavailable inside the virtual environment at {0}".format(venv_path))

    run_with_retry([str(python_path), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])


def install_python_requirements(venv_path: Path, requirements_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError("Missing venv python executable at {0}".format(python_path))
    if not requirements_path.exists():
        raise BootstrapError("Missing requirements file at {0}".format(requirements_path))

    log("Installing backend Python dependencies from {0}".format(requirements_path))
    run_with_retry([str(python_path), "-m", "pip", "install", "-r", str(requirements_path)])


def has_node() -> bool:
    return executable_version(["node", "--version"]) is not None


def has_npm() -> bool:
    return executable_version(["npm", "--version"]) is not None


def ensure_node_toolchain() -> None:
    node_version = executable_version(["node", "--version"])
    npm_version = executable_version(["npm", "--version"])
    node_major = node_major_version(node_version)
    if node_version and npm_version and node_major is not None and node_major >= MIN_NODE_MAJOR:
        log("Node available: {0}".format(node_version))
        log("npm available: {0}".format(npm_version))
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError("Node.js/npm missing and no supported Linux package manager was found.")

    if manager == "apt-get":
        log(
            "Installing/upgrading Node.js via NodeSource because COSMIC WhatsApp bridge requires Node.js {0}+.".format(
                MIN_NODE_MAJOR
            )
        )
        install_system_packages(manager, ["ca-certificates", "curl"])
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as temp_script:
            setup_script = Path(temp_script.name)
        try:
            run_with_retry(["curl", "-fsSL", "https://deb.nodesource.com/setup_20.x", "-o", str(setup_script)])
            run(["bash", str(setup_script)], use_sudo=True)
            run_with_retry(["apt-get", "install", "-y", "nodejs"], use_sudo=True)
        finally:
            setup_script.unlink(missing_ok=True)

        node_version = executable_version(["node", "--version"])
        npm_version = executable_version(["npm", "--version"])
        node_major = node_major_version(node_version)
        if node_version and npm_version and node_major is not None and node_major >= MIN_NODE_MAJOR:
            log("Node available: {0}".format(node_version))
            log("npm available: {0}".format(npm_version))
            return

        raise BootstrapError(
            "Node.js upgrade did not produce a supported runtime. Need Node.js {0}+, got {1}.".format(
                MIN_NODE_MAJOR,
                node_version or "missing",
            )
        )

    packages: list[str] = []
    if not node_version:
        package_name = PACKAGE_NAMES["nodejs"].get(manager)
        if not package_name:
            raise BootstrapError("No Node.js package mapping for package manager: {0}".format(manager))
        packages.append(package_name)
    if not npm_version:
        package_name = PACKAGE_NAMES["npm"].get(manager)
        if not package_name:
            raise BootstrapError("No npm package mapping for package manager: {0}".format(manager))
        packages.append(package_name)

    log("Installing Node.js toolchain via {0}: {1}".format(manager, ", ".join(packages)))
    install_system_packages(manager, packages)

    node_version = executable_version(["node", "--version"])
    npm_version = executable_version(["npm", "--version"])
    node_major = node_major_version(node_version)
    if not node_version or not npm_version or node_major is None or node_major < MIN_NODE_MAJOR:
        raise BootstrapError(
            "Node.js/npm are still unavailable or too old after installation attempts. Need Node.js {0}+, got {1}.".format(
                MIN_NODE_MAJOR,
                node_version or "missing",
            )
        )


def load_package_json(package_json: Path) -> dict:
    try:
        return json.loads(package_json.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError("Missing package.json at {0}".format(package_json)) from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError("Invalid package.json at {0}: {1}".format(package_json, exc)) from exc


def service_env_specs(
    system_env_dir: Optional[Path] = None,
    *,
    include_memory: bool = False,
) -> List[Tuple[Path, Path]]:
    system_env_dir = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
    specs = [
        (BACKEND_ROOT / "gateway.env", system_env_dir / "gateway.env"),
        (BACKEND_ROOT / "model_router.env", system_env_dir / "model-router.env"),
        (BACKEND_ROOT / "orchestrator.env", system_env_dir / "orchestrator.env"),
        (DEFAULT_BRIDGE_DIR / ".env", system_env_dir / "whatsapp-bridge.env"),
    ]
    if include_memory:
        specs.append((BACKEND_ROOT / "memory.env", system_env_dir / "memory.env"))
    return specs


def fallback_service_env_specs(
    system_env_dir: Optional[Path] = None,
    *,
    include_memory: bool = False,
) -> List[Tuple[Path, Path]]:
    system_env_dir = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
    specs = [
        (BACKEND_ROOT / "gateway.env.example", system_env_dir / "gateway.env"),
        (BACKEND_ROOT / "model_router.env.example", system_env_dir / "model-router.env"),
        (BACKEND_ROOT / "orchestrator.env.example", system_env_dir / "orchestrator.env"),
        (DEFAULT_BRIDGE_DIR / ".env.example", system_env_dir / "whatsapp-bridge.env"),
    ]
    if include_memory:
        specs.append((BACKEND_ROOT / "memory.env.example", system_env_dir / "memory.env"))
    return specs


def resolve_effective_service_env_sources(
    system_env_dir: Optional[Path] = None,
    *,
    include_memory: bool = False,
) -> List[Tuple[Path, Path]]:
    effective_sources: list[Tuple[Path, Path]] = []
    fallback_sources = {
        dest: source
        for source, dest in fallback_service_env_specs(
            system_env_dir,
            include_memory=include_memory,
        )
    }
    for source, dest in service_env_specs(system_env_dir, include_memory=include_memory):
        effective_sources.append((source if source.exists() else fallback_sources[dest], dest))
    return effective_sources


def first_meaningful_value(*values: Optional[str]) -> Optional[str]:
    for value in values:
        normalized = meaningful_env_value(value)
        if normalized is not None:
            return normalized
    return None


def build_service_env_overrides(
    effective_sources: Sequence[Tuple[Path, Path]],
    *,
    include_memory: bool = False,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    existing_env_by_name = existing_env_by_name or {}
    external_env_by_name = external_env_by_name or {}
    gateway_source = next(source for source, dest in effective_sources if dest.name == "gateway.env")
    model_router_source = next(source for source, dest in effective_sources if dest.name == "model-router.env")
    orchestrator_source = next(source for source, dest in effective_sources if dest.name == "orchestrator.env")
    bridge_source = next(source for source, dest in effective_sources if dest.name == "whatsapp-bridge.env")
    memory_source = None
    if include_memory:
        memory_source = next(source for source, dest in effective_sources if dest.name == "memory.env")
    gateway_data = parse_env_text(gateway_source.read_text(encoding="utf-8"))
    model_router_data = parse_env_text(model_router_source.read_text(encoding="utf-8"))
    orchestrator_data = parse_env_text(orchestrator_source.read_text(encoding="utf-8"))
    bridge_data = parse_env_text(bridge_source.read_text(encoding="utf-8"))
    memory_data = parse_env_text(memory_source.read_text(encoding="utf-8")) if memory_source is not None else {}
    gateway_existing = existing_env_by_name.get("gateway.env", {})
    model_router_existing = existing_env_by_name.get("model-router.env", {})
    orchestrator_existing = existing_env_by_name.get("orchestrator.env", {})
    bridge_existing = existing_env_by_name.get("whatsapp-bridge.env", {})
    memory_existing = existing_env_by_name.get("memory.env", {})
    gateway_external = external_env_by_name.get("gateway.env", {})
    model_router_external = external_env_by_name.get("model-router.env", {})
    orchestrator_external = external_env_by_name.get("orchestrator.env", {})
    bridge_external = external_env_by_name.get("whatsapp-bridge.env", {})
    memory_external = external_env_by_name.get("memory.env", {})

    shared_internal_token = first_meaningful_value(
        gateway_external.get("GATEWAY_INTERNAL_TOKEN"),
        orchestrator_external.get("GATEWAY_INTERNAL_TOKEN"),
        bridge_external.get("GATEWAY_INTERNAL_TOKEN"),
        memory_external.get("GATEWAY_INTERNAL_TOKEN"),
        gateway_existing.get("GATEWAY_INTERNAL_TOKEN"),
        bridge_existing.get("GATEWAY_INTERNAL_TOKEN"),
        gateway_data.get("GATEWAY_INTERNAL_TOKEN"),
        orchestrator_existing.get("GATEWAY_INTERNAL_TOKEN"),
        orchestrator_data.get("GATEWAY_INTERNAL_TOKEN"),
        bridge_data.get("GATEWAY_INTERNAL_TOKEN"),
        memory_existing.get("GATEWAY_INTERNAL_TOKEN"),
        memory_data.get("GATEWAY_INTERNAL_TOKEN"),
        secrets.token_urlsafe(32),
    )
    signing_secret = first_meaningful_value(
        gateway_external.get("GATEWAY_SIGNING_SECRET"),
        orchestrator_external.get("GATEWAY_SIGNING_SECRET"),
        gateway_existing.get("GATEWAY_SIGNING_SECRET"),
        orchestrator_existing.get("GATEWAY_SIGNING_SECRET"),
        gateway_data.get("GATEWAY_SIGNING_SECRET"),
        orchestrator_data.get("GATEWAY_SIGNING_SECRET"),
        secrets.token_urlsafe(32),
    )
    bridge_token = first_meaningful_value(
        gateway_external.get("WHATSAPP_BRIDGE_TOKEN"),
        bridge_external.get("WHATSAPP_BRIDGE_TOKEN"),
        gateway_existing.get("WHATSAPP_BRIDGE_TOKEN"),
        bridge_existing.get("WHATSAPP_BRIDGE_TOKEN"),
        gateway_data.get("WHATSAPP_BRIDGE_TOKEN"),
        bridge_data.get("WHATSAPP_BRIDGE_TOKEN"),
        secrets.token_urlsafe(32),
    )
    local_api_token = first_meaningful_value(
        gateway_external.get("GATEWAY_LOCAL_API_TOKEN"),
        gateway_existing.get("GATEWAY_LOCAL_API_TOKEN"),
        gateway_data.get("GATEWAY_LOCAL_API_TOKEN"),
        secrets.token_urlsafe(24),
    )
    whatsapp_auth_dir = first_meaningful_value(
        bridge_external.get("WHATSAPP_AUTH_DIR"),
        bridge_existing.get("WHATSAPP_AUTH_DIR"),
        bridge_data.get("WHATSAPP_AUTH_DIR"),
        str(DEFAULT_WHATSAPP_AUTH_DIR),
    )
    shared_anthropic_api_key = first_meaningful_value(
        gateway_external.get("ANTHROPIC_API_KEY"),
        orchestrator_external.get("ANTHROPIC_API_KEY"),
        gateway_existing.get("ANTHROPIC_API_KEY"),
        orchestrator_existing.get("ANTHROPIC_API_KEY"),
        gateway_existing.get("HAIKU_API_KEY"),
        gateway_data.get("ANTHROPIC_API_KEY"),
        orchestrator_data.get("ANTHROPIC_API_KEY"),
        gateway_data.get("HAIKU_API_KEY"),
    )
    perplexity_api_key = first_meaningful_value(
        gateway_external.get("PERPLEXITY_API_KEY"),
        gateway_existing.get("PERPLEXITY_API_KEY"),
        gateway_data.get("PERPLEXITY_API_KEY"),
    )
    groq_api_key = first_meaningful_value(
        model_router_external.get("GROQ_API_KEY"),
        model_router_existing.get("GROQ_API_KEY"),
        model_router_data.get("GROQ_API_KEY"),
    )
    gateway_public_host = first_meaningful_value(
        gateway_external.get("GATEWAY_PUBLIC_HOST"),
        gateway_existing.get("GATEWAY_PUBLIC_HOST"),
        gateway_data.get("GATEWAY_PUBLIC_HOST"),
    )
    haiku_model = first_meaningful_value(
        gateway_external.get("HAIKU_MODEL"),
        gateway_existing.get("HAIKU_MODEL"),
        gateway_data.get("HAIKU_MODEL"),
    )
    opus_model = first_meaningful_value(
        orchestrator_external.get("ANTHROPIC_MODEL"),
        orchestrator_external.get("OPUS_MODEL"),
        orchestrator_existing.get("ANTHROPIC_MODEL"),
        orchestrator_data.get("ANTHROPIC_MODEL"),
    )
    memory_url = first_meaningful_value(
        gateway_external.get("COSMIC_MEMORY_URL"),
        gateway_existing.get("COSMIC_MEMORY_URL"),
        gateway_data.get("COSMIC_MEMORY_URL"),
    )
    memory_perplexity_api_key = first_meaningful_value(
        memory_external.get("PERPLEXITY_API_KEY"),
        memory_existing.get("PERPLEXITY_API_KEY"),
        memory_data.get("PERPLEXITY_API_KEY"),
        gateway_external.get("PERPLEXITY_API_KEY"),
        gateway_existing.get("PERPLEXITY_API_KEY"),
        gateway_data.get("PERPLEXITY_API_KEY"),
    )
    memory_xai_api_key = first_meaningful_value(
        memory_external.get("XAI_API_KEY"),
        memory_existing.get("XAI_API_KEY"),
        memory_data.get("XAI_API_KEY"),
    )
    memory_data_dir = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_DATA_DIR"),
        memory_existing.get("COSMIC_MEMORY_DATA_DIR"),
        memory_data.get("COSMIC_MEMORY_DATA_DIR"),
        str(DEFAULT_MEMORY_DATA_DIR),
    )
    memory_sync_on_startup = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_SYNC_ON_STARTUP"),
        memory_existing.get("COSMIC_MEMORY_SYNC_ON_STARTUP"),
        memory_data.get("COSMIC_MEMORY_SYNC_ON_STARTUP"),
        "true",
    )
    memory_graph_extract_enabled = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED"),
        memory_data.get("COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED"),
        "false",
    )
    memory_graph_backend = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_BACKEND"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_BACKEND"),
        memory_data.get("COSMIC_MEMORY_GRAPH_BACKEND"),
        "memory",
    )
    memory_graph_sync_on_startup = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_SYNC_ON_STARTUP"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_SYNC_ON_STARTUP"),
        memory_data.get("COSMIC_MEMORY_GRAPH_SYNC_ON_STARTUP"),
        "true",
    )
    memory_graph_deterministic_enabled = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        memory_data.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        "true",
    )
    memory_primary_user_display_name = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        memory_existing.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        memory_data.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        "",
    )

    overrides = {
        "gateway.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token or secrets.token_urlsafe(32),
            "GATEWAY_LOCAL_API_TOKEN": local_api_token or secrets.token_urlsafe(24),
            "GATEWAY_SIGNING_SECRET": signing_secret or secrets.token_urlsafe(32),
            "WHATSAPP_BRIDGE_TOKEN": bridge_token or secrets.token_urlsafe(32),
            "ANTHROPIC_API_KEY": shared_anthropic_api_key or "<anthropic-api-key>",
            "PERPLEXITY_API_KEY": perplexity_api_key or "<perplexity-api-key>",
            "GATEWAY_PUBLIC_HOST": gateway_public_host or "<gateway.user.example.com>",
            "HAIKU_MODEL": haiku_model or "claude-haiku-4-5",
        },
        "model-router.env": {
            "GROQ_API_KEY": groq_api_key or "<groq-api-key>",
        },
        "orchestrator.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token or secrets.token_urlsafe(32),
            "GATEWAY_SIGNING_SECRET": signing_secret or secrets.token_urlsafe(32),
            "ANTHROPIC_API_KEY": shared_anthropic_api_key or "<anthropic-api-key>",
            "ANTHROPIC_MODEL": opus_model or "claude-opus-4-6",
        },
        "whatsapp-bridge.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token or secrets.token_urlsafe(32),
            "WHATSAPP_BRIDGE_TOKEN": bridge_token or secrets.token_urlsafe(32),
            "WHATSAPP_AUTH_DIR": whatsapp_auth_dir or str(DEFAULT_WHATSAPP_AUTH_DIR),
        },
    }
    if include_memory:
        overrides["gateway.env"]["COSMIC_MEMORY_URL"] = memory_url or "http://127.0.0.1:8090"
        overrides["memory.env"] = {
            "PERPLEXITY_API_KEY": memory_perplexity_api_key or "<perplexity-api-key>",
            "XAI_API_KEY": memory_xai_api_key or "",
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token or secrets.token_urlsafe(32),
            "COSMIC_MEMORY_INTERNAL_TOKEN": shared_internal_token or secrets.token_urlsafe(32),
            "COSMIC_MEMORY_DATA_DIR": memory_data_dir or str(DEFAULT_MEMORY_DATA_DIR),
            "COSMIC_MEMORY_SYNC_ON_STARTUP": memory_sync_on_startup or "true",
            "COSMIC_MEMORY_GRAPH_SYNC_ON_STARTUP": memory_graph_sync_on_startup or "true",
            "COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED": memory_graph_deterministic_enabled or "true",
            "COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED": memory_graph_extract_enabled or "false",
            "COSMIC_MEMORY_GRAPH_BACKEND": memory_graph_backend or "memory",
            "COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME": memory_primary_user_display_name or "",
        }
    return overrides


def materialize_bootstrap_env_files(
    search_roots: Sequence[Path],
    system_env_dir: Path,
    *,
    bootstrap_token: str,
    supabase_url: str,
    supabase_anon_key: str,
    include_memory: bool = False,
) -> List[Path]:
    setup_env_files(search_roots)
    external_env_by_name = fetch_bootstrap_env_payload(
        bootstrap_token=bootstrap_token,
        supabase_url=supabase_url,
        supabase_anon_key=supabase_anon_key,
    )
    effective_sources = resolve_effective_service_env_sources(
        system_env_dir,
        include_memory=include_memory,
    )
    repo_source_by_name = {
        dest.name: source
        for source, dest in service_env_specs(
            system_env_dir,
            include_memory=include_memory,
        )
    }
    existing_env_by_name: Dict[str, Dict[str, str]] = {}
    for source_path, dest_path in effective_sources:
        repo_path = repo_source_by_name[dest_path.name]
        if repo_path.exists():
            existing_env_by_name[dest_path.name] = parse_env_text(repo_path.read_text(encoding="utf-8"))
        elif source_path.exists():
            existing_env_by_name[dest_path.name] = parse_env_text(source_path.read_text(encoding="utf-8"))

    overrides_by_dest = build_service_env_overrides(
        effective_sources,
        include_memory=include_memory,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=external_env_by_name,
    )

    written: list[Path] = []
    for source_path, dest_path in effective_sources:
        repo_path = repo_source_by_name[dest_path.name]
        raw_source_path = repo_path if repo_path.exists() else source_path
        rendered = render_env_with_overrides(
            raw_source_path.read_text(encoding="utf-8"),
            overrides_by_dest.get(dest_path.name, {}),
        )
        repo_path.write_text(rendered, encoding="utf-8")
        written.append(repo_path)
        log("Materialized repo env file from Supabase bootstrap payload: {0}".format(repo_path))
    return written


def install_service_env_files(system_env_dir: Path, *, include_memory: bool = False) -> List[Path]:
    if not is_linux():
        raise BootstrapError("System env provisioning currently targets Linux VMs only.")

    effective_sources = resolve_effective_service_env_sources(
        system_env_dir,
        include_memory=include_memory,
    )

    validate_required_service_env_files(effective_sources)

    overrides_by_dest = build_service_env_overrides(
        effective_sources,
        include_memory=include_memory,
    )

    run(["install", "-d", "-m", "755", str(system_env_dir)], use_sudo=True)

    installed: list[Path] = []
    for source_path, dest_path in effective_sources:
        if dest_path.exists():
            log("System env file already exists: {0}".format(dest_path))
            continue

        raw = source_path.read_text(encoding="utf-8")
        rendered = render_env_with_overrides(raw, overrides_by_dest.get(dest_path.name, {}))
        install_text_file(dest_path, rendered, mode="600", use_sudo=True)

        installed.append(dest_path)
        log("Installed system env file: {0}".format(dest_path))

    return installed


def install_whatsapp_bridge_dependencies(bridge_dir: Path) -> None:
    package_json = bridge_dir / "package.json"
    if not bridge_dir.exists():
        raise BootstrapError("WhatsApp bridge directory does not exist: {0}".format(bridge_dir))
    if not package_json.exists():
        raise BootstrapError("Missing WhatsApp bridge package.json at {0}".format(package_json))

    ensure_node_toolchain()
    package_data = load_package_json(package_json)

    package_lock = bridge_dir / "package-lock.json"
    install_command = ["npm", "install"]
    if package_lock.exists():
        # Keep bridge installs reproducible on the VM and avoid mutating the
        # committed lockfile during routine provisioning.
        install_command = ["npm", "ci"]

    log("Installing WhatsApp bridge dependencies in {0}".format(bridge_dir))
    run_with_retry(install_command, check=True, capture_output=False, cwd=bridge_dir)

    scripts = package_data.get("scripts") if isinstance(package_data.get("scripts"), dict) else {}
    if "build" in scripts:
        log("Running WhatsApp bridge build script in {0}".format(bridge_dir))
        run(["npm", "run", "build"], check=True, capture_output=False, cwd=bridge_dir)


def current_service_user() -> str:
    return os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()


def install_systemd_units(
    template_dir: Path,
    *,
    enable_units: bool = False,
    start_units: bool = False,
    include_optional_templates: Optional[Sequence[str]] = None,
    extra_enable_units: Optional[Sequence[str]] = None,
    include_memory_env: bool = False,
) -> List[str]:
    if not is_linux():
        raise BootstrapError("Systemd install currently targets Linux VMs only.")
    if shutil.which("systemctl") is None:
        raise BootstrapError("systemctl not found. This host does not appear to use systemd.")
    if not template_dir.exists():
        raise BootstrapError("Systemd template directory does not exist: {0}".format(template_dir))

    selected_optional_templates = set(include_optional_templates or [])
    templates = sorted(
        path
        for path in template_dir.glob("*.example")
        if path.is_file()
        and (
            path.name not in OPTIONAL_SYSTEMD_TEMPLATES
            or path.name in selected_optional_templates
        )
    )
    if not templates:
        raise BootstrapError("No systemd template files found in {0}".format(template_dir))

    install_service_env_files(DEFAULT_SYSTEM_ENV_DIR, include_memory=include_memory_env)

    installed_names: list[str] = []
    service_user = current_service_user()
    run(
        [
            "install",
            "-d",
            "-m",
            "700",
            "-o",
            service_user,
            "-g",
            service_user,
            str(DEFAULT_WHATSAPP_AUTH_DIR),
        ],
        use_sudo=True,
    )
    with tempfile.TemporaryDirectory(prefix="cosmic-systemd-") as temp_dir:
        temp_path = Path(temp_dir)
        for template in templates:
            rendered_name = template.name[: -len(".example")]
            rendered_path = temp_path / rendered_name
            rendered_text = (
                template.read_text(encoding="utf-8")
                .replace("<BACKEND_ROOT>", str(BACKEND_ROOT))
                .replace("<SERVICE_USER>", service_user)
                .replace("<SERVICE_GROUP>", service_user)
            )
            rendered_path.write_text(rendered_text, encoding="utf-8")
            run(
                ["install", "-m", "644", str(rendered_path), "/etc/systemd/system/{0}".format(rendered_name)],
                use_sudo=True,
            )
            installed_names.append(rendered_name)

    run(["systemctl", "daemon-reload"], use_sudo=True)

    if enable_units and installed_names:
        preferred_units = [name for name in installed_names if name.endswith(".target")] or installed_names
        additional_units = [name for name in (extra_enable_units or []) if name]
        run(["systemctl", "enable", *preferred_units, *additional_units], use_sudo=True)
        if start_units:
            run(["systemctl", "restart", *preferred_units, *additional_units], use_sudo=True)

    return installed_names


def doctor(
    venv_path: Path,
    requirements_path: Path,
    bridge_dir: Path,
    systemd_template_dir: Path,
    env_search_roots: Sequence[Path],
) -> None:
    manager = detect_package_manager()
    current_version = sys.version_info[:3]
    current_supported = current_version[:2] >= MIN_PYTHON
    env_examples = discover_env_example_files(env_search_roots)

    print("COSMIC Backend bootstrap doctor")
    print("  platform           : {0}".format(sys.platform))
    print("  current python     : {0}.{1}.{2} ({3})".format(
        current_version[0], current_version[1], current_version[2], sys.executable
    ))
    print("  python supported   : {0}".format("yes" if current_supported else "no"))
    print("  package manager    : {0}".format(manager or "not found"))
    print("  pip available      : {0}".format("yes" if has_pip() else "no"))
    print("  venv available     : {0}".format("yes" if has_venv_module() else "no"))
    print("  target venv        : {0}".format(venv_path))
    print("  venv exists        : {0}".format("yes" if venv_python_path(venv_path).exists() else "no"))
    print("  requirements file  : {0}".format(requirements_path if requirements_path.exists() else "missing"))
    print("  node available     : {0}".format(executable_version(["node", "--version"]) or "no"))
    print("  npm available      : {0}".format(executable_version(["npm", "--version"]) or "no"))
    print("  bridge dir         : {0}".format(bridge_dir if bridge_dir.exists() else "missing"))
    print("  bridge package     : {0}".format(
        (bridge_dir / "package.json") if (bridge_dir / "package.json").exists() else "missing"
    ))
    print("  env search roots   : {0}".format(", ".join(str(path) for path in env_search_roots)))
    print("  env templates      : {0}".format(len(env_examples)))
    print("  systemd templates  : {0}".format(systemd_template_dir if systemd_template_dir.exists() else "missing"))

    effective_sources: list[Tuple[Path, Path]] = []
    fallback_sources = {dest: source for source, dest in fallback_service_env_specs(DEFAULT_SYSTEM_ENV_DIR)}
    for source, dest in service_env_specs(DEFAULT_SYSTEM_ENV_DIR):
        effective_sources.append((source if source.exists() else fallback_sources[dest], dest))

    for source_path, dest_path in effective_sources:
        required_keys = REQUIRED_SERVICE_ENV_KEYS.get(dest_path.name, ())
        if not required_keys:
            continue
        missing_keys = missing_required_env_keys(source_path, required_keys)
        print(
            "  required env check : {0} -> {1}".format(
                source_path,
                "ok" if not missing_keys else "missing {0}".format(", ".join(missing_keys)),
            )
        )

    supported = find_supported_python()
    if supported and Path(supported).resolve() != Path(sys.executable).resolve():
        print("  alternate python   : {0}".format(supported))


def setup_env_files(search_roots: Sequence[Path]) -> List[Path]:
    created = ensure_env_files(search_roots)
    if not created:
        log("No new env files were created from templates.")
    return created


def sync_repo_env_files(search_roots: Sequence[Path]) -> List[Path]:
    synced: list[Path] = []
    for example_path in discover_env_example_files(search_roots):
        target_path = example_target_path(example_path)
        source_raw = example_path.read_text(encoding="utf-8")
        changed_keys = sync_env_file(target_path, source_raw=source_raw, create_missing=True, use_sudo=False, mode="644")
        if changed_keys or target_path.exists():
            synced.append(target_path)
    return synced


def sync_service_env_files(system_env_dir: Path, *, include_memory: bool = False) -> List[Path]:
    if not is_linux():
        raise BootstrapError("System env syncing currently targets Linux VMs only.")

    run(["install", "-d", "-m", "755", str(system_env_dir)], use_sudo=True)
    effective_sources = resolve_effective_service_env_sources(
        system_env_dir,
        include_memory=include_memory,
    )
    existing_env_by_name: Dict[str, Dict[str, str]] = {}
    for _source_path, dest_path in effective_sources:
        if not dest_path.exists():
            continue
        existing_env_by_name[dest_path.name] = parse_env_text(read_text_file(dest_path, use_sudo=True))

    overrides_by_dest = build_service_env_overrides(
        effective_sources,
        include_memory=include_memory,
        existing_env_by_name=existing_env_by_name,
    )

    synced: list[Path] = []
    for source_path, dest_path in effective_sources:
        raw = source_path.read_text(encoding="utf-8")
        rendered = render_env_with_overrides(raw, overrides_by_dest.get(dest_path.name, {}))
        changed_keys = sync_env_file(
            dest_path,
            source_raw=rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(dest_path)
    return synced


def sync_env(
    search_roots: Sequence[Path],
    system_env_dir: Path,
    *,
    bootstrap_token: Optional[str] = None,
    supabase_url: str = DEFAULT_SUPABASE_URL,
    supabase_anon_key: str = DEFAULT_SUPABASE_ANON_KEY,
    include_memory: bool = False,
) -> None:
    sync_repo_env_files(search_roots)
    if meaningful_env_value(bootstrap_token) is not None:
        materialize_bootstrap_env_files(
            search_roots,
            system_env_dir,
            bootstrap_token=bootstrap_token or "",
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            include_memory=include_memory,
        )
    if not is_linux():
        log("Skipping /etc/cosmic env sync on non-Linux host.")
        return
    sync_service_env_files(system_env_dir, include_memory=include_memory)


def setup_python(venv_path: Path, requirements_path: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    ensure_python3_available()
    ensure_pip()
    ensure_venv_support()
    ensure_virtualenv(venv_path)
    upgrade_venv_pip(venv_path)
    install_python_requirements(venv_path, requirements_path)


def setup_whatsapp_bridge(bridge_dir: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    install_whatsapp_bridge_dependencies(bridge_dir)


def resolve_memory_repo_dir(configured_path: Path | None) -> Path | None:
    if configured_path is None:
        return None
    resolved = configured_path.expanduser().resolve()
    if not resolved.exists():
        raise BootstrapError("cosmic-memory repo directory does not exist: {0}".format(resolved))
    if not (resolved / "pyproject.toml").exists():
        raise BootstrapError("cosmic-memory repo is missing pyproject.toml: {0}".format(resolved))
    return resolved


def setup_cosmic_memory(venv_path: Path, memory_repo_dir: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError("Missing venv python executable at {0}".format(python_path))

    memory_repo = resolve_memory_repo_dir(memory_repo_dir)
    if memory_repo is None:
        return

    install_target = "{0}[qdrant-local,graph,llm]".format(memory_repo)
    log("Installing cosmic-memory package from {0}".format(memory_repo))
    run_with_retry(
        [str(python_path), "-m", "pip", "install", "--upgrade", install_target],
        cwd=memory_repo,
    )


def setup_vm_edge(
    edge_setup_script: Path,
    gateway_env_path: Path,
    *,
    gateway_host: str | None = None,
    force: bool = False,
    skip_if_unconfigured: bool = False,
) -> None:
    if not is_linux():
        raise BootstrapError("VM edge setup currently targets Linux VMs only.")
    if not edge_setup_script.exists():
        raise BootstrapError("VM edge setup script does not exist: {0}".format(edge_setup_script))

    command = [sys.executable, str(edge_setup_script), "--gateway-env", str(gateway_env_path)]
    if gateway_host:
        command.extend(["--gateway-host", gateway_host])
    if force:
        command.append("--force")
    if skip_if_unconfigured:
        command.append("--skip-if-unconfigured")
    command.append("setup")
    run(command)


def bootstrap(
    venv_path: Path,
    requirements_path: Path,
    bridge_dir: Path,
    env_search_roots: Sequence[Path],
    *,
    edge_setup_script: Path | None = None,
    gateway_env_path: Path | None = None,
    gateway_host: str | None = None,
    skip_edge: bool = False,
    force_edge: bool = False,
    bootstrap_token: Optional[str] = None,
    supabase_url: str = DEFAULT_SUPABASE_URL,
    supabase_anon_key: str = DEFAULT_SUPABASE_ANON_KEY,
    memory_repo_dir: Path | None = None,
) -> None:
    enable_memory = memory_repo_dir is not None
    setup_env_files(env_search_roots)
    if meaningful_env_value(bootstrap_token) is not None:
        materialize_bootstrap_env_files(
            env_search_roots,
            DEFAULT_SYSTEM_ENV_DIR,
            bootstrap_token=bootstrap_token or "",
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            include_memory=enable_memory,
        )
    setup_local_redis(resolve_configured_redis_url())
    setup_python(venv_path, requirements_path)
    if memory_repo_dir is not None:
        setup_cosmic_memory(venv_path, memory_repo_dir)
    setup_whatsapp_bridge(bridge_dir)
    if not skip_edge and edge_setup_script is not None and gateway_env_path is not None:
        setup_vm_edge(
            edge_setup_script,
            gateway_env_path,
            gateway_host=gateway_host,
            force=force_edge,
            skip_if_unconfigured=True,
        )

    print("")
    print("Bootstrap complete")
    print("  python : {0}".format(sys.executable))
    print("  venv   : {0}".format(venv_path))
    print("  deps   : {0}".format(requirements_path))
    print("  bridge : {0}".format(bridge_dir))
    print("  next   : source {0}/bin/activate".format(venv_path))


def provision_vm(
    venv_path: Path,
    requirements_path: Path,
    bridge_dir: Path,
    env_search_roots: Sequence[Path],
    systemd_template_dir: Path,
    *,
    enable_units: bool = True,
    start_units: bool = True,
    edge_setup_script: Path | None = None,
    gateway_env_path: Path | None = None,
    gateway_host: str | None = None,
    skip_edge: bool = False,
    force_edge: bool = False,
    bootstrap_token: Optional[str] = None,
    supabase_url: str = DEFAULT_SUPABASE_URL,
    supabase_anon_key: str = DEFAULT_SUPABASE_ANON_KEY,
    memory_repo_dir: Path | None = None,
) -> None:
    enable_memory = memory_repo_dir is not None
    bootstrap(
        venv_path,
        requirements_path,
        bridge_dir,
        env_search_roots,
        edge_setup_script=edge_setup_script,
        gateway_env_path=gateway_env_path,
        gateway_host=gateway_host,
        skip_edge=skip_edge,
        force_edge=force_edge,
        bootstrap_token=bootstrap_token,
        supabase_url=supabase_url,
        supabase_anon_key=supabase_anon_key,
        memory_repo_dir=memory_repo_dir,
    )
    if enable_memory:
        run(
            [
                "install",
                "-d",
                "-m",
                "700",
                "-o",
                current_service_user(),
                "-g",
                current_service_user(),
                str(DEFAULT_MEMORY_DATA_DIR),
            ],
            use_sudo=True,
        )
    installed = install_systemd_units(
        systemd_template_dir,
        enable_units=enable_units,
        start_units=start_units,
        include_optional_templates=["cosmic-memory.service.example"] if enable_memory else [],
        extra_enable_units=["cosmic-memory.service"] if enable_units and enable_memory else [],
        include_memory_env=enable_memory,
    )

    print("")
    print("VM provisioning complete")
    for unit_name in installed:
        print("  unit   : {0}".format(unit_name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap COSMIC Backend on a Linux VM.")
    parser.add_argument(
        "--venv-path",
        default=str(DEFAULT_VENV_PATH),
        help="Virtual environment path. Default: %(default)s",
    )
    parser.add_argument(
        "--requirements",
        default=str(DEFAULT_REQUIREMENTS_PATH),
        help="Backend requirements file path. Default: %(default)s",
    )
    parser.add_argument(
        "--bridge-dir",
        default=str(DEFAULT_BRIDGE_DIR),
        help="WhatsApp bridge directory. Default: %(default)s",
    )
    parser.add_argument(
        "--systemd-template-dir",
        default=str(DEFAULT_SYSTEMD_TEMPLATE_DIR),
        help="Directory containing systemd *.example templates. Default: %(default)s",
    )
    parser.add_argument(
        "--env-search-root",
        action="append",
        default=[],
        help="Directory root to scan recursively for *.env.example templates. Can be passed multiple times.",
    )
    parser.add_argument(
        "--edge-script",
        default=str(DEFAULT_EDGE_SETUP_SCRIPT),
        help="Path to vm_edge_setup.py. Default: %(default)s",
    )
    parser.add_argument(
        "--gateway-env-path",
        default=str(DEFAULT_GATEWAY_ENV_PATH),
        help="Path to gateway.env used by vm_edge_setup.py. Default: %(default)s",
    )
    parser.add_argument(
        "--gateway-host",
        default="",
        help="Public DNS hostname for the Gateway edge. Overrides GATEWAY_PUBLIC_HOST in gateway.env.",
    )
    parser.add_argument(
        "--memory-repo-dir",
        default="",
        help="Optional local path to the cosmic-memory repo. When set, bootstrap installs the package, materializes memory.env, and provisions cosmic-memory.service.",
    )
    parser.add_argument(
        "--skip-edge",
        action="store_true",
        help="Skip invoking vm_edge_setup.py during bootstrap/provision-vm.",
    )
    parser.add_argument(
        "--force-edge",
        action="store_true",
        help="Pass --force to vm_edge_setup.py when overwriting an existing unmanaged Caddyfile.",
    )
    parser.add_argument(
        "--bootstrap-token",
        default="",
        help="One-time Supabase bootstrap token. Prefer setting COSMIC_BOOTSTRAP_TOKEN to avoid shell history.",
    )
    parser.add_argument(
        "--supabase-url",
        default=DEFAULT_SUPABASE_URL,
        help="Supabase base URL used for bootstrap env fetch. Default: %(default)s",
    )
    parser.add_argument(
        "--supabase-anon-key",
        default=DEFAULT_SUPABASE_ANON_KEY,
        help="Supabase anon key used for bootstrap env fetch. Defaults to the committed public project key.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="Check current bootstrap prerequisites without changing the system.")
    subparsers.add_parser(
        "bootstrap",
        help="Install missing prerequisites, create the backend virtual environment, install Python deps, and install WhatsApp bridge deps.",
    )
    subparsers.add_parser(
        "setup-python",
        help="Prepare the Python runtime, create/update the backend virtual environment, and install backend Python dependencies.",
    )
    subparsers.add_parser(
        "setup-whatsapp-bridge",
        help="Install Node.js bridge dependencies in bridges/whatsapp_bridge.",
    )
    subparsers.add_parser(
        "setup-env",
        help="Create missing env files from committed *.env.example templates.",
    )
    subparsers.add_parser(
        "sync-env",
        help="Append missing keys from committed env templates without overwriting current values. On Linux VMs, also updates existing /etc/cosmic env files.",
    )
    subparsers.add_parser(
        "fetch-bootstrap-env",
        help="Fetch per-VM env values from Supabase using a one-time bootstrap token and materialize the repo env files.",
    )
    subparsers.add_parser(
        "setup-edge",
        help="Install/configure the public Caddy/TLS edge using vm_edge_setup.py.",
    )
    install_systemd_parser = subparsers.add_parser(
        "install-systemd",
        help="Install systemd unit templates into /etc/systemd/system and reload systemd.",
    )
    install_systemd_parser.add_argument(
        "--enable",
        action="store_true",
        help="Enable the installed units (prefers the target unit when present).",
    )
    install_systemd_parser.add_argument(
        "--start",
        action="store_true",
        help="Restart the enabled units after installation.",
    )
    subparsers.add_parser(
        "provision-vm",
        help="Create env files, install Python and bridge deps, provision /etc/cosmic envs, install systemd units, enable them, and start the backend target.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "bootstrap"
    venv_path = Path(args.venv_path).expanduser().resolve()
    requirements_path = Path(args.requirements).expanduser().resolve()
    bridge_dir = Path(args.bridge_dir).expanduser().resolve()
    systemd_template_dir = Path(args.systemd_template_dir).expanduser().resolve()
    edge_setup_script = Path(args.edge_script).expanduser().resolve()
    gateway_env_path = Path(args.gateway_env_path).expanduser().resolve()
    gateway_host = getattr(args, "gateway_host", "").strip() or None
    memory_repo_dir = (
        resolve_memory_repo_dir(Path(args.memory_repo_dir))
        if meaningful_env_value(getattr(args, "memory_repo_dir", "")) is not None
        else None
    )
    bootstrap_token = (
        meaningful_env_value(getattr(args, "bootstrap_token", ""))
        or meaningful_env_value(os.getenv("COSMIC_BOOTSTRAP_TOKEN"))
    )
    supabase_url = meaningful_env_value(getattr(args, "supabase_url", "")) or DEFAULT_SUPABASE_URL
    supabase_anon_key = (
        meaningful_env_value(getattr(args, "supabase_anon_key", ""))
        or meaningful_env_value(os.getenv("COSMIC_SUPABASE_ANON_KEY"))
        or DEFAULT_SUPABASE_ANON_KEY
    )
    env_search_roots = [
        Path(item).expanduser().resolve()
        for item in (args.env_search_root or [])
    ] or list(DEFAULT_ENV_SEARCH_ROOTS)

    try:
        if command == "doctor":
            doctor(venv_path, requirements_path, bridge_dir, systemd_template_dir, env_search_roots)
        elif command == "setup-env":
            setup_env_files(env_search_roots)
            if bootstrap_token is not None:
                materialize_bootstrap_env_files(
                    env_search_roots,
                    DEFAULT_SYSTEM_ENV_DIR,
                    bootstrap_token=bootstrap_token,
                    supabase_url=supabase_url,
                    supabase_anon_key=supabase_anon_key,
                    include_memory=memory_repo_dir is not None,
                )
        elif command == "sync-env":
            sync_env(
                env_search_roots,
                DEFAULT_SYSTEM_ENV_DIR,
                bootstrap_token=bootstrap_token,
                supabase_url=supabase_url,
                supabase_anon_key=supabase_anon_key,
                include_memory=memory_repo_dir is not None,
            )
        elif command == "fetch-bootstrap-env":
            if bootstrap_token is None:
                raise BootstrapError("fetch-bootstrap-env requires --bootstrap-token or COSMIC_BOOTSTRAP_TOKEN.")
            materialize_bootstrap_env_files(
                env_search_roots,
                DEFAULT_SYSTEM_ENV_DIR,
                bootstrap_token=bootstrap_token,
                supabase_url=supabase_url,
                supabase_anon_key=supabase_anon_key,
                include_memory=memory_repo_dir is not None,
            )
        elif command == "setup-python":
            setup_python(venv_path, requirements_path)
            if memory_repo_dir is not None:
                setup_cosmic_memory(venv_path, memory_repo_dir)
        elif command == "setup-whatsapp-bridge":
            setup_whatsapp_bridge(bridge_dir)
        elif command == "setup-edge":
            setup_vm_edge(
                edge_setup_script,
                gateway_env_path,
                gateway_host=gateway_host,
                force=bool(getattr(args, "force_edge", False)),
                skip_if_unconfigured=False,
            )
        elif command == "install-systemd":
            installed = install_systemd_units(
                systemd_template_dir,
                enable_units=bool(getattr(args, "enable", False)),
                start_units=bool(getattr(args, "start", False)),
                include_optional_templates=["cosmic-memory.service.example"] if memory_repo_dir is not None else [],
                extra_enable_units=["cosmic-memory.service"] if memory_repo_dir is not None and bool(getattr(args, "enable", False)) else [],
                include_memory_env=memory_repo_dir is not None,
            )
            print("Installed systemd units:")
            for unit_name in installed:
                print("  - {0}".format(unit_name))
        elif command == "provision-vm":
            provision_vm(
                venv_path,
                requirements_path,
                bridge_dir,
                env_search_roots,
                systemd_template_dir,
                edge_setup_script=edge_setup_script,
                gateway_env_path=gateway_env_path,
                gateway_host=gateway_host,
                skip_edge=bool(getattr(args, "skip_edge", False)),
                force_edge=bool(getattr(args, "force_edge", False)),
                bootstrap_token=bootstrap_token,
                supabase_url=supabase_url,
                supabase_anon_key=supabase_anon_key,
                memory_repo_dir=memory_repo_dir,
            )
        else:
            bootstrap(
                venv_path,
                requirements_path,
                bridge_dir,
                env_search_roots,
                edge_setup_script=edge_setup_script,
                gateway_env_path=gateway_env_path,
                gateway_host=gateway_host,
                skip_edge=bool(getattr(args, "skip_edge", False)),
                force_edge=bool(getattr(args, "force_edge", False)),
                bootstrap_token=bootstrap_token,
                supabase_url=supabase_url,
                supabase_anon_key=supabase_anon_key,
                memory_repo_dir=memory_repo_dir,
            )
    except BootstrapError as exc:
        print("Bootstrap failed: {0}".format(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            "Bootstrap failed while running: {0} (exit={1})".format(
                command_str(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]),
                exc.returncode,
            ),
            file=sys.stderr,
        )
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
