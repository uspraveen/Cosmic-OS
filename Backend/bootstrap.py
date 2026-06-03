#!/usr/bin/env python3
"""
Bootstrap helper for COSMIC Backend VM setup.

This script is meant to be the first thing run on a Linux VM after copying or
cloning the backend repo. It handles Python readiness, pip availability,
virtual environment creation, backend dependency installation, WhatsApp bridge
dependency setup, and the production memory stack bootstrap (public
cosmic-memory checkout plus local Neo4j provisioning) when memory is enabled.
It can also invoke the dedicated VM edge setup script for Caddy/TLS when a
public hostname is configured. It is intentionally structured so future setup
steps can be added without turning it into an unmaintainable script.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import getpass
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import pwd
except ImportError:  # pragma: no cover - unavailable on Windows
    pwd = None

from shared import (
    AgentEmailIntegrationStore,
    agent_email_integration_is_configured,
    agent_email_integration_is_disabled,
)
from shared.cursor_cli_config import ensure_cursor_cli_non_fast_config


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
DEFAULT_MEMORY_REPO_DIR = BACKEND_ROOT.parent.parent / "cosmic-memory"
DEFAULT_MEMORY_REPO_URL = "https://github.com/uspraveen/cosmic-memory.git"
DEFAULT_MEMORY_REPO_REF = "main"
DEFAULT_SYSTEM_ENV_DIR = Path("/etc/cosmic")
DEFAULT_WHATSAPP_AUTH_DIR = Path("/var/lib/cosmic/whatsapp/auth")
DEFAULT_DIAGRAM_PUPPETEER_CACHE_DIR = Path("/var/lib/cosmic/diagram-agent/puppeteer")
DEFAULT_MEMORY_DATA_DIR = Path("/var/lib/cosmic/memory")
DEFAULT_ALPHA_CURSOR_HOME = Path("/var/lib/cosmic/alpha/homes/cursor")
DEFAULT_NEO4J_APT_KEY_URL = "https://debian.neo4j.com/neotechnology.gpg.key"
DEFAULT_NEO4J_APT_KEYRING_PATH = Path("/etc/apt/keyrings/neotechnology.gpg")
DEFAULT_NEO4J_APT_SOURCE_PATH = Path("/etc/apt/sources.list.d/neo4j.list")
DEFAULT_NEO4J_APT_SOURCE = (
    "deb [signed-by={keyring}] https://debian.neo4j.com stable latest"
).format(keyring=DEFAULT_NEO4J_APT_KEYRING_PATH)
DEFAULT_NEO4J_CONFIG_PATH = Path("/etc/neo4j/neo4j.conf")
DEFAULT_NEO4J_URI = "bolt://127.0.0.1:7687"
DEFAULT_NEO4J_USERNAME = "neo4j"
DEFAULT_NEO4J_DATABASE = "neo4j"
DEFAULT_NEO4J_SERVICE_NAME = "neo4j"
FIRECRAWL_AGENT_ENV_NAME = "firecrawl-web-scrape-agent.env"
FIRECRAWL_AGENT_SERVICE_NAME = "cosmic-firecrawl-web-scrape-agent.service"
FIRECRAWL_AGENT_ID = "cosmic/firecrawl-web-scrape-agent:1.0.0"
FIRECRAWL_AGENT_DEFAULT_INSTANCE_ID = "firecrawl-web-scrape-agent-1"
DOCS_PARSER_AGENT_ENV_NAME = "docs-parser-agent.env"
DOCS_PARSER_AGENT_SERVICE_NAME = "cosmic-docs-parser-agent.service"
DOCS_PARSER_AGENT_ID = "cosmic/docs-parser-agent:1.0.0"
DOCS_PARSER_AGENT_DEFAULT_INSTANCE_ID = "docs-parser-agent-1"
X_TWITTER_SEARCH_AGENT_ENV_NAME = "x-twitter-search-agent.env"
X_TWITTER_SEARCH_AGENT_SERVICE_NAME = "cosmic-x-twitter-search-agent.service"
X_TWITTER_SEARCH_AGENT_ID = "cosmic/x-twitter-search-agent:1.0.0"
X_TWITTER_SEARCH_AGENT_DEFAULT_INSTANCE_ID = "x-twitter-search-agent-1"
TABULAR_AGENT_ENV_NAME = "tabular-agent.env"
TABULAR_AGENT_SERVICE_NAME = "cosmic-tabular-agent.service"
TABULAR_AGENT_ID = "cosmic/tabular-agent:1.0.0"
TABULAR_AGENT_DEFAULT_INSTANCE_ID = "tabular-agent-1"
EMAIL_AGENT_ENV_NAME = "email-agent.env"
EMAIL_AGENT_SERVICE_NAME = "cosmic-email-agent.service"
EMAIL_AGENT_ID = "cosmic/email-agent:1.0.0"
EMAIL_AGENT_DEFAULT_INSTANCE_ID = "email-agent-1"
IMAGE_GENERATOR_AGENT_ENV_NAME = "image-generator-agent.env"
IMAGE_GENERATOR_AGENT_SERVICE_NAME = "cosmic-image-generator-agent.service"
IMAGE_GENERATOR_AGENT_ID = "cosmic/image-generator-agent:1.0.0"
IMAGE_GENERATOR_AGENT_DEFAULT_INSTANCE_ID = "image-generator-agent-1"
CALENDAR_AGENT_ENV_NAME = "calendar-agent.env"
CALENDAR_AGENT_SERVICE_NAME = "cosmic-calendar-agent.service"
CALENDAR_AGENT_ID = "cosmic/calendar-agent:1.0.0"
CALENDAR_AGENT_DEFAULT_INSTANCE_ID = "calendar-agent-1"
GMAIL_AGENT_ENV_NAME = "gmail-agent.env"
GMAIL_AGENT_SERVICE_NAME = "cosmic-gmail-agent.service"
GMAIL_AGENT_ID = "cosmic/gmail-agent:1.0.0"
GMAIL_AGENT_DEFAULT_INSTANCE_ID = "gmail-agent-1"
GOOGLE_DOCS_AGENT_ENV_NAME = "google-docs-agent.env"
GOOGLE_DOCS_AGENT_SERVICE_NAME = "cosmic-google-docs-agent.service"
GOOGLE_DOCS_AGENT_ID = "cosmic/google-docs-agent:1.0.0"
GOOGLE_DOCS_AGENT_DEFAULT_INSTANCE_ID = "google-docs-agent-1"
GOOGLE_SHEETS_AGENT_ENV_NAME = "google-sheets-agent.env"
GOOGLE_SHEETS_AGENT_SERVICE_NAME = "cosmic-google-sheets-agent.service"
GOOGLE_SHEETS_AGENT_ID = "cosmic/google-sheets-agent:1.0.0"
GOOGLE_SHEETS_AGENT_DEFAULT_INSTANCE_ID = "google-sheets-agent-1"
DIAGRAM_AGENT_ENV_NAME = "diagram-agent.env"
DIAGRAM_AGENT_SERVICE_NAME = "cosmic-diagram-agent.service"
DIAGRAM_AGENT_ID = "cosmic/diagram-agent:1.0.0"
DIAGRAM_AGENT_DEFAULT_INSTANCE_ID = "diagram-agent-1"
MAP_AGENT_ENV_NAME = "map-agent.env"
MAP_AGENT_SERVICE_NAME = "cosmic-map-agent.service"
MAP_AGENT_ID = "cosmic/map-agent:1.0.0"
MAP_AGENT_DEFAULT_INSTANCE_ID = "map-agent-1"
SLIDE_AGENT_ENV_NAME = "slide-agent.env"
SLIDE_AGENT_SERVICE_NAME = "cosmic-slide-agent.service"
SLIDE_AGENT_ID = "cosmic/slide-agent:1.0.0"
SLIDE_AGENT_DEFAULT_INSTANCE_ID = "slide-agent-1"
ALPHA_AGENT_ENV_NAME = "alpha-agent.env"
ALPHA_AGENT_SERVICE_NAME = "cosmic-alpha-agent.service"
ALPHA_AGENT_ID = "cosmic/alpha-agent:1.0.0"
ALPHA_AGENT_DEFAULT_INSTANCE_ID = "alpha-agent-1"
CRITICAL_VENV_IMPORT_CHECKS: Tuple[Tuple[str, str], ...] = (
    ("docling", "docs parser runtime"),
    ("playwright", "slide HTML renderer"),
    ("reportlab", "slide PDF/vector renderer"),
    ("svglib", "slide SVG renderer"),
)
DEFAULT_POST_PROVISION_TIMEOUT_SEC = 120.0
DEFAULT_POST_PROVISION_POLL_INTERVAL_SEC = 2.0
CORE_BACKEND_SERVICE_UNITS = (
    "cosmic-model-router.service",
    "cosmic-orchestrator.service",
    "cosmic-gateway.service",
    "cosmic-docs-parser-agent.service",
    "cosmic-tabular-agent.service",
    "cosmic-calendar-agent.service",
    "cosmic-gmail-agent.service",
    "cosmic-google-docs-agent.service",
    "cosmic-google-sheets-agent.service",
    "cosmic-diagram-agent.service",
    "cosmic-map-agent.service",
    "cosmic-slide-agent.service",
    "cosmic-whatsapp-bridge.service",
)
DEFAULT_SUPABASE_URL = "https://hluenippcdiejenmteen.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhsdWVuaXBwY2RpZWplbm10ZWVuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTE4MzYwOTMsImV4cCI6MjA2NzQxMjA5M30."
    "dm6YO4B9SAQ8hnGtR-OZS7jn5FcL-zz4s4XxP-TyCpk"
)
DEFAULT_SUPABASE_BOOTSTRAP_RPC = "consume_bootstrap_token"
DEFAULT_COSMIC_MAIL_PROVISION_FUNCTION = "provision-cosmic-mail-org"
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
MERMAID_CLI_PACKAGE = "@mermaid-js/mermaid-cli"
OPENAI_CODEX_CLI_PACKAGE = "@openai/codex"
CURSOR_CLI_INSTALL_URL = "https://cursor.com/install"
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
    "poppler": {
        "apt-get": "poppler-utils",
        "dnf": "poppler-utils",
        "yum": "poppler-utils",
        "apk": "poppler-utils",
    },
}
SLIDE_PYTHON_BUILD_PACKAGE_NAMES: Dict[str, Tuple[str, ...]] = {
    "apt-get": ("pkg-config", "libcairo2-dev"),
    "dnf": ("pkgconf-pkg-config", "cairo-devel"),
    "yum": ("pkgconfig", "cairo-devel"),
    "apk": ("pkgconf", "cairo-dev"),
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


def run_redacted(
    command: Sequence[str],
    *,
    display_command: Sequence[str],
    use_sudo: bool = False,
    capture_output: bool = False,
    check: bool = True,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    full_command = list(command)
    display = list(display_command)
    if use_sudo and not is_root():
        sudo_path = shutil.which("sudo")
        if not sudo_path:
            raise BootstrapError(
                "System package install requires root or sudo. Missing sudo for command: {0}".format(
                    command_str(display_command)
                )
            )
        full_command = [sudo_path] + full_command
        display = [sudo_path] + display

    log("Running: {0}".format(command_str(display)))
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


def parse_os_release(path: Path = Path("/etc/os-release")) -> Dict[str, str]:
    if not path.exists():
        return {}
    parsed: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"').strip("'")
    return parsed


def is_ubuntu_host() -> bool:
    os_release = parse_os_release()
    host_id = os_release.get("ID", "").strip().lower()
    id_like = os_release.get("ID_LIKE", "").strip().lower()
    return host_id == "ubuntu" or "ubuntu" in {part.strip() for part in id_like.split()}


def executable_version(command: Sequence[str]) -> Optional[str]:
    try:
        result = run(command, capture_output=True)
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return None
    return (result.stdout or "").strip() or (result.stderr or "").strip() or None


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


def install_text_file(
    path: Path, content: str, *, mode: str = "600", use_sudo: bool = False
) -> None:
    with tempfile.NamedTemporaryFile(
        "w", delete=False, encoding="utf-8", newline="\n"
    ) as tmp:
        tmp.write(content)
        temp_path = Path(tmp.name)
    try:
        run(["install", "-m", mode, str(temp_path), str(path)], use_sudo=use_sudo)
    finally:
        temp_path.unlink(missing_ok=True)


def install_bytes_file(
    path: Path, content: bytes, *, mode: str = "644", use_sudo: bool = False
) -> None:
    with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
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


def replace_placeholder_env_entries(
    existing_raw: str, source_raw: str
) -> Tuple[str, List[str]]:
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


def merge_missing_env_entries(
    existing_raw: str, source_raw: str
) -> Tuple[str, List[str]]:
    existing_reconciled, replaced_keys = replace_placeholder_env_entries(
        existing_raw, source_raw
    )
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
    appended_sections = [
        "\n".join(block).rstrip() for _key, block in missing_blocks if block
    ]
    appended_body = "\n\n".join(
        section for section in appended_sections if section
    ).strip()
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


def render_assignment_overrides(raw: str, overrides: Dict[str, str]) -> str:
    rendered_lines: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            rendered_lines.append(line)
            continue

        key, _value = line.split("=", 1)
        assignment_key = key.strip()
        if assignment_key in overrides:
            rendered_lines.append(
                "{0}={1}".format(assignment_key, overrides[assignment_key])
            )
            seen.add(assignment_key)
        else:
            rendered_lines.append(line)

    for key, value in overrides.items():
        if key not in seen:
            rendered_lines.append("{0}={1}".format(key, value))

    return "\n".join(rendered_lines).rstrip() + "\n"


def sync_assignment_file(
    target_path: Path,
    *,
    overrides: Dict[str, str],
    create_missing: bool = True,
    use_sudo: bool = False,
    mode: str = "644",
) -> None:
    existing_raw = ""
    if target_path.exists():
        existing_raw = read_text_file(target_path, use_sudo=use_sudo)
    elif not create_missing:
        raise BootstrapError(
            "Cannot update missing config file: {0}".format(target_path)
        )

    rendered = render_assignment_overrides(existing_raw, overrides)
    install_text_file(target_path, rendered, mode=mode, use_sudo=use_sudo)


def meaningful_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.startswith("<") and normalized.endswith(">"):
        return None
    return normalized


def firecrawl_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "firecrawl_web_scrape"


def firecrawl_agent_repo_env_path() -> Path:
    return firecrawl_agent_repo_dir() / "agent.env"


def firecrawl_agent_repo_env_example_path() -> Path:
    return firecrawl_agent_repo_dir() / "agent.env.example"


def firecrawl_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / FIRECRAWL_AGENT_ENV_NAME
    )


def resolve_firecrawl_agent_env_source() -> Path:
    repo_env = firecrawl_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return firecrawl_agent_repo_env_example_path()


def build_firecrawl_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_firecrawl_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    firecrawl_api_key = first_meaningful_value(
        external_env.get("FIRECRAWL_API_KEY"),
        existing_env.get("FIRECRAWL_API_KEY"),
        source_data.get("FIRECRAWL_API_KEY"),
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        FIRECRAWL_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or FIRECRAWL_AGENT_DEFAULT_INSTANCE_ID,
    }
    if firecrawl_api_key is not None:
        overrides["FIRECRAWL_API_KEY"] = firecrawl_api_key

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return firecrawl_agent_system_env_path(system_env_dir), rendered, rendered_data


def firecrawl_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return meaningful_env_value(env_values.get("FIRECRAWL_API_KEY")) is not None


def read_firecrawl_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = firecrawl_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def visual_enhancement_repo_env_path() -> Path:
    return BACKEND_ROOT / "visual_enhancement.env"


def visual_enhancement_repo_env_example_path() -> Path:
    return BACKEND_ROOT / "visual_enhancement.env.example"


def visual_enhancement_system_env_path(
    system_env_dir: Optional[Path] = None,
) -> Path:
    return (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "visual_enhancement.env"


def resolve_visual_enhancement_env_source() -> Path:
    repo_env = visual_enhancement_repo_env_path()
    if repo_env.exists():
        return repo_env
    return visual_enhancement_repo_env_example_path()


def build_visual_enhancement_env_rendered(
    *,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_visual_enhancement_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    env_name = "visual_enhancement.env"
    existing_env = (existing_env_by_name or {}).get(env_name, {})
    external_env = (external_env_by_name or {}).get(env_name, {})
    firecrawl_external_env = (external_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})
    slide_external_env = (external_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    orchestrator_external_env = (external_env_by_name or {}).get("orchestrator.env", {})
    firecrawl_existing_env = (existing_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})
    slide_existing_env = (existing_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    orchestrator_existing_env = (existing_env_by_name or {}).get("orchestrator.env", {})

    def read_optional_env(path: Path) -> Dict[str, str]:
        if not path.exists():
            return {}
        try:
            if is_linux():
                return parse_env_text(read_text_file(path, use_sudo=True))
            return parse_env_text(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    if not firecrawl_existing_env:
        firecrawl_existing_env = read_optional_env(
            firecrawl_agent_system_env_path(system_env_dir)
        )
    if not slide_existing_env:
        slide_existing_env = read_optional_env(slide_agent_system_env_path(system_env_dir))
    if not orchestrator_existing_env:
        orchestrator_existing_env = read_optional_env(
            (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "orchestrator.env"
        )

    def pick_visual(names: Sequence[str], default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            *(external_env.get(name) for name in names),
            *(existing_env.get(name) for name in names),
            *(source_data.get(name) for name in names),
            default,
        )

    enabled = pick_visual(("VISUAL_ENHANCEMENT_ENABLED",), "true")
    max_visuals = pick_visual(("VISUAL_ENHANCEMENT_MAX_VISUALS_PER_TURN",), "2")
    max_image_slots = pick_visual(
        ("VISUAL_ENHANCEMENT_MAX_IMAGE_SLOTS_PER_TURN",), "1"
    )
    max_chart_slots = pick_visual(
        ("VISUAL_ENHANCEMENT_MAX_CHART_SLOTS_PER_TURN",), "1"
    )
    max_concurrent_sidecars = pick_visual(
        ("VISUAL_ENHANCEMENT_MAX_CONCURRENT_SIDECARS",), "2"
    )
    image_slot_timeout_ms = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SLOT_TIMEOUT_MS",), "6000"
    )
    chart_slot_timeout_ms = pick_visual(
        ("VISUAL_ENHANCEMENT_CHART_SLOT_TIMEOUT_MS",), "4000"
    )
    finalization_grace_ms = pick_visual(
        ("VISUAL_ENHANCEMENT_FINALIZATION_GRACE_MS",), "750"
    )
    image_source_page_limit = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SOURCE_PAGE_LIMIT",), "3"
    )
    image_candidate_limit = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_CANDIDATE_LIMIT",), "12"
    )
    image_max_bytes = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_MAX_BYTES",), "8388608"
    )
    image_verify_top_k = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_VERIFY_TOP_K",), "1"
    )
    image_min_confidence = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_MIN_CONFIDENCE",), "0.58"
    )
    image_search_enabled = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED",), "true"
    )
    image_search_base_url = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL",), "https://www.bing.com/images/search"
    )
    image_search_timeout_sec = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC",), "5"
    )
    image_search_result_limit = pick_visual(
        ("VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT",), "8"
    )
    chart_max_points = pick_visual(
        ("VISUAL_ENHANCEMENT_CHART_MAX_POINTS",), "200"
    )
    chart_max_bytes = pick_visual(
        ("VISUAL_ENHANCEMENT_CHART_MAX_BYTES",), "4194304"
    )
    download_timeout_sec = pick_visual(
        ("VISUAL_ENHANCEMENT_DOWNLOAD_TIMEOUT_SEC",), "6"
    )

    firecrawl_api_key = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY"),
        existing_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY"),
        source_data.get("VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY"),
        orchestrator_external_env.get("FIRECRAWL_API_KEY"),
        orchestrator_existing_env.get("FIRECRAWL_API_KEY"),
        firecrawl_external_env.get("FIRECRAWL_API_KEY"),
        firecrawl_existing_env.get("FIRECRAWL_API_KEY"),
    )
    firecrawl_base_url = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL"),
        existing_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL"),
        orchestrator_external_env.get("FIRECRAWL_API_BASE_URL"),
        orchestrator_existing_env.get("FIRECRAWL_API_BASE_URL"),
        firecrawl_external_env.get("FIRECRAWL_API_BASE_URL"),
        firecrawl_existing_env.get("FIRECRAWL_API_BASE_URL"),
        source_data.get("VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL"),
        "https://api.firecrawl.dev",
    )
    firecrawl_request_timeout_sec = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        existing_env.get("VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        firecrawl_external_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        source_data.get("VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        "20",
    )

    fireworks_api_key = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        orchestrator_external_env.get("MODEL_API_KEY"),
        orchestrator_external_env.get("FIREWORKS_API_KEY"),
        orchestrator_external_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_existing_env.get("MODEL_API_KEY"),
        orchestrator_existing_env.get("FIREWORKS_API_KEY"),
        orchestrator_existing_env.get("OPENAI_COMPAT_API_KEY"),
        slide_external_env.get("MODEL_API_KEY"),
        slide_external_env.get("SLIDE_AGENT_FIREWORKS_API_KEY"),
        slide_external_env.get("FIREWORKS_API_KEY"),
        slide_external_env.get("OPENAI_COMPAT_API_KEY"),
        slide_existing_env.get("MODEL_API_KEY"),
        slide_existing_env.get("SLIDE_AGENT_FIREWORKS_API_KEY"),
        slide_existing_env.get("FIREWORKS_API_KEY"),
        slide_existing_env.get("OPENAI_COMPAT_API_KEY"),
    )
    fireworks_base_url = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL"),
        orchestrator_external_env.get("MODEL_BASE_URL"),
        orchestrator_external_env.get("FIREWORKS_BASE_URL"),
        orchestrator_external_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_existing_env.get("MODEL_BASE_URL"),
        orchestrator_existing_env.get("FIREWORKS_BASE_URL"),
        orchestrator_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_external_env.get("MODEL_BASE_URL"),
        slide_external_env.get("SLIDE_AGENT_FIREWORKS_BASE_URL"),
        slide_external_env.get("FIREWORKS_BASE_URL"),
        slide_external_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_existing_env.get("MODEL_BASE_URL"),
        slide_existing_env.get("SLIDE_AGENT_FIREWORKS_BASE_URL"),
        slide_existing_env.get("FIREWORKS_BASE_URL"),
        slide_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL"),
        "https://api.fireworks.ai/inference/v1",
    )
    fireworks_model = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_MODEL"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_MODEL"),
        orchestrator_external_env.get("FIREWORKS_KIMI_MODEL"),
        orchestrator_external_env.get("MODEL_NAME"),
        orchestrator_existing_env.get("FIREWORKS_KIMI_MODEL"),
        orchestrator_existing_env.get("MODEL_NAME"),
        slide_external_env.get("FIREWORKS_KIMI_MODEL"),
        slide_external_env.get("MODEL_NAME"),
        slide_external_env.get("SLIDE_AGENT_FIREWORKS_MODEL"),
        slide_existing_env.get("FIREWORKS_KIMI_MODEL"),
        slide_existing_env.get("MODEL_NAME"),
        slide_existing_env.get("SLIDE_AGENT_FIREWORKS_MODEL"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_MODEL"),
        "accounts/fireworks/models/kimi-k2p6",
    )
    fireworks_vision_model = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL"),
        fireworks_model,
    )
    fireworks_reasoning_effort = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT"),
        "low",
    )
    fireworks_timeout_sec = first_meaningful_value(
        external_env.get("VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC"),
        existing_env.get("VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC"),
        orchestrator_external_env.get("MODEL_TIMEOUT_SEC"),
        orchestrator_existing_env.get("MODEL_TIMEOUT_SEC"),
        slide_external_env.get("MODEL_TIMEOUT_SEC"),
        slide_external_env.get("SLIDE_AGENT_FIREWORKS_TIMEOUT_SEC"),
        slide_existing_env.get("MODEL_TIMEOUT_SEC"),
        slide_existing_env.get("SLIDE_AGENT_FIREWORKS_TIMEOUT_SEC"),
        source_data.get("VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC"),
        "20",
    )

    overrides = {
        "VISUAL_ENHANCEMENT_ENABLED": enabled or "true",
        "VISUAL_ENHANCEMENT_MAX_VISUALS_PER_TURN": max_visuals or "2",
        "VISUAL_ENHANCEMENT_MAX_IMAGE_SLOTS_PER_TURN": max_image_slots or "1",
        "VISUAL_ENHANCEMENT_MAX_CHART_SLOTS_PER_TURN": max_chart_slots or "1",
        "VISUAL_ENHANCEMENT_MAX_CONCURRENT_SIDECARS": max_concurrent_sidecars or "2",
        "VISUAL_ENHANCEMENT_IMAGE_SLOT_TIMEOUT_MS": image_slot_timeout_ms or "6000",
        "VISUAL_ENHANCEMENT_CHART_SLOT_TIMEOUT_MS": chart_slot_timeout_ms or "4000",
        "VISUAL_ENHANCEMENT_FINALIZATION_GRACE_MS": finalization_grace_ms or "750",
        "VISUAL_ENHANCEMENT_IMAGE_SOURCE_PAGE_LIMIT": image_source_page_limit or "3",
        "VISUAL_ENHANCEMENT_IMAGE_CANDIDATE_LIMIT": image_candidate_limit or "12",
        "VISUAL_ENHANCEMENT_IMAGE_MAX_BYTES": image_max_bytes or "8388608",
        "VISUAL_ENHANCEMENT_IMAGE_VERIFY_TOP_K": image_verify_top_k or "1",
        "VISUAL_ENHANCEMENT_IMAGE_MIN_CONFIDENCE": image_min_confidence or "0.58",
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED": image_search_enabled or "true",
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL": image_search_base_url
        or "https://www.bing.com/images/search",
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC": image_search_timeout_sec or "5",
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT": image_search_result_limit or "8",
        "VISUAL_ENHANCEMENT_CHART_MAX_POINTS": chart_max_points or "200",
        "VISUAL_ENHANCEMENT_CHART_MAX_BYTES": chart_max_bytes or "4194304",
        "VISUAL_ENHANCEMENT_DOWNLOAD_TIMEOUT_SEC": download_timeout_sec or "6",
        "VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL": firecrawl_base_url
        or "https://api.firecrawl.dev",
        "VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC": firecrawl_request_timeout_sec
        or "20",
        "VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL": fireworks_base_url
        or "https://api.fireworks.ai/inference/v1",
        "VISUAL_ENHANCEMENT_FIREWORKS_MODEL": fireworks_model
        or "accounts/fireworks/models/kimi-k2p6",
        "VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL": fireworks_vision_model
        or fireworks_model
        or "accounts/fireworks/models/kimi-k2p6",
        "VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT": fireworks_reasoning_effort
        or "low",
        "VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC": fireworks_timeout_sec or "20",
    }
    if firecrawl_api_key is not None:
        overrides["VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY"] = firecrawl_api_key
    if fireworks_api_key is not None:
        overrides["VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"] = fireworks_api_key

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return visual_enhancement_system_env_path(system_env_dir), rendered, rendered_data


def docs_parser_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "docs_parser"


def docs_parser_agent_repo_env_path() -> Path:
    return docs_parser_agent_repo_dir() / "agent.env"


def docs_parser_agent_repo_env_example_path() -> Path:
    return docs_parser_agent_repo_dir() / "agent.env.example"


def docs_parser_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR)
        / "agents"
        / DOCS_PARSER_AGENT_ENV_NAME
    )


def resolve_docs_parser_agent_env_source() -> Path:
    repo_env = docs_parser_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return docs_parser_agent_repo_env_example_path()


def build_docs_parser_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_docs_parser_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(DOCS_PARSER_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(DOCS_PARSER_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        DOCS_PARSER_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or DOCS_PARSER_AGENT_DEFAULT_INSTANCE_ID,
    }

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return docs_parser_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_docs_parser_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = docs_parser_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def x_twitter_search_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "x_twitter_search"


def x_twitter_search_agent_repo_env_path() -> Path:
    return x_twitter_search_agent_repo_dir() / "agent.env"


def x_twitter_search_agent_repo_env_example_path() -> Path:
    return x_twitter_search_agent_repo_dir() / "agent.env.example"


def x_twitter_search_agent_system_env_path(
    system_env_dir: Optional[Path] = None,
) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR)
        / "agents"
        / X_TWITTER_SEARCH_AGENT_ENV_NAME
    )


def resolve_x_twitter_search_agent_env_source() -> Path:
    repo_env = x_twitter_search_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return x_twitter_search_agent_repo_env_example_path()


def build_x_twitter_search_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_x_twitter_search_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(X_TWITTER_SEARCH_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(X_TWITTER_SEARCH_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    xai_api_key = first_meaningful_value(
        external_env.get("XAI_API_KEY"),
        existing_env.get("XAI_API_KEY"),
        source_data.get("XAI_API_KEY"),
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        X_TWITTER_SEARCH_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or X_TWITTER_SEARCH_AGENT_DEFAULT_INSTANCE_ID,
    }
    if xai_api_key is not None:
        overrides["XAI_API_KEY"] = xai_api_key

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return (
        x_twitter_search_agent_system_env_path(system_env_dir),
        rendered,
        rendered_data,
    )


def x_twitter_search_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return meaningful_env_value(env_values.get("XAI_API_KEY")) is not None


def read_x_twitter_search_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = x_twitter_search_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def tabular_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "tabular_agent"


def tabular_agent_repo_env_path() -> Path:
    return tabular_agent_repo_dir() / "agent.env"


def tabular_agent_repo_env_example_path() -> Path:
    return tabular_agent_repo_dir() / "agent.env.example"


def tabular_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / TABULAR_AGENT_ENV_NAME
    )


def resolve_tabular_agent_env_source() -> Path:
    repo_env = tabular_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return tabular_agent_repo_env_example_path()


def build_tabular_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_tabular_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(TABULAR_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(TABULAR_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    orchestrator_url = first_meaningful_value(
        external_env.get("TABULAR_AGENT_ORCHESTRATOR_URL"),
        external_env.get("ORCHESTRATOR_URL"),
        existing_env.get("TABULAR_AGENT_ORCHESTRATOR_URL"),
        existing_env.get("ORCHESTRATOR_URL"),
        source_data.get("TABULAR_AGENT_ORCHESTRATOR_URL"),
        source_data.get("ORCHESTRATOR_URL"),
        "http://127.0.0.1:8743",
    )
    internal_llm_api_key = first_meaningful_value(
        external_env.get("TABULAR_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("TABULAR_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("TABULAR_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("TABULAR_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("TABULAR_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("TABULAR_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )
    internal_llm_model = first_meaningful_value(
        external_env.get("TABULAR_AGENT_INTERNAL_LLM_MODEL"),
        existing_env.get("TABULAR_AGENT_INTERNAL_LLM_MODEL"),
        source_data.get("TABULAR_AGENT_INTERNAL_LLM_MODEL"),
        "gpt-5-mini",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        TABULAR_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or TABULAR_AGENT_DEFAULT_INSTANCE_ID,
        "TABULAR_AGENT_ORCHESTRATOR_URL": orchestrator_url or "http://127.0.0.1:8743",
        "TABULAR_AGENT_ORCHESTRATOR_INTERNAL_TOKEN": shared_internal_token,
        "TABULAR_AGENT_INTERNAL_LLM_MODEL": internal_llm_model or "gpt-5-mini",
    }
    if internal_llm_api_key is not None:
        overrides["TABULAR_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["TABULAR_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return tabular_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_tabular_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = tabular_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def email_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "email_agent"


def email_agent_repo_env_path() -> Path:
    return email_agent_repo_dir() / "agent.env"


def email_agent_repo_env_example_path() -> Path:
    return email_agent_repo_dir() / "agent.env.example"


def email_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / EMAIL_AGENT_ENV_NAME


def resolve_email_agent_env_source() -> Path:
    repo_env = email_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return email_agent_repo_env_example_path()


def agent_email_integrations_db_path() -> Path:
    return BACKEND_ROOT / "gateway" / "agent_email_integrations.db"


def read_agent_email_integration_state() -> Tuple[str, Dict[str, str]]:
    try:
        store = AgentEmailIntegrationStore(agent_email_integrations_db_path())
        record = store.get_primary()
    except Exception:
        return "absent", {}
    if record is None:
        return "absent", {}
    if agent_email_integration_is_disabled(record):
        return "disabled", {}
    if not agent_email_integration_is_configured(record):
        return "absent", {}
    return "configured", {
        "COSMIC_MAIL_BASE_URL": str(record.base_url or "").strip(),
        "COSMIC_MAIL_API_TOKEN": str(record.api_token or "").strip(),
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS": str(
            record.primary_mailbox_address or ""
        ).strip(),
    }


def read_agent_email_integration_record() -> Dict[str, str]:
    _, payload = read_agent_email_integration_state()
    return payload


def email_agent_enabled_via_env_or_integration(email_env: Dict[str, str]) -> bool:
    integration_state, _ = read_agent_email_integration_state()
    if integration_state == "configured":
        return True
    if integration_state == "disabled":
        return False
    return email_agent_is_configured(email_env)


def build_email_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_email_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(EMAIL_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(EMAIL_AGENT_ENV_NAME, {})
    integration_state, integration_store_env = read_agent_email_integration_state()

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    if integration_state == "disabled":
        cosmic_mail_base_url = first_meaningful_value(
            external_env.get("COSMIC_MAIL_BASE_URL"),
            "",
        )
        cosmic_mail_api_token = first_meaningful_value(
            external_env.get("COSMIC_MAIL_API_TOKEN"),
            "",
        )
        primary_mailbox_address = first_meaningful_value(
            external_env.get("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"),
            "",
        )
    else:
        cosmic_mail_base_url = first_meaningful_value(
            external_env.get("COSMIC_MAIL_BASE_URL"),
            integration_store_env.get("COSMIC_MAIL_BASE_URL"),
            existing_env.get("COSMIC_MAIL_BASE_URL"),
            source_data.get("COSMIC_MAIL_BASE_URL"),
        )
        cosmic_mail_api_token = first_meaningful_value(
            external_env.get("COSMIC_MAIL_API_TOKEN"),
            integration_store_env.get("COSMIC_MAIL_API_TOKEN"),
            existing_env.get("COSMIC_MAIL_API_TOKEN"),
            source_data.get("COSMIC_MAIL_API_TOKEN"),
        )
        primary_mailbox_address = first_meaningful_value(
            external_env.get("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"),
            integration_store_env.get("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"),
            existing_env.get("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"),
            source_data.get("COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"),
        )
    internal_llm_api_key = first_meaningful_value(
        external_env.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("EMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("EMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("EMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )
    internal_llm_model = first_meaningful_value(
        external_env.get("EMAIL_AGENT_INTERNAL_LLM_MODEL"),
        existing_env.get("EMAIL_AGENT_INTERNAL_LLM_MODEL"),
        source_data.get("EMAIL_AGENT_INTERNAL_LLM_MODEL"),
        "gpt-5-mini",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        EMAIL_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or EMAIL_AGENT_DEFAULT_INSTANCE_ID,
        "EMAIL_AGENT_INTERNAL_LLM_MODEL": internal_llm_model or "gpt-5-mini",
    }
    if cosmic_mail_base_url is not None:
        overrides["COSMIC_MAIL_BASE_URL"] = cosmic_mail_base_url
    if cosmic_mail_api_token is not None:
        overrides["COSMIC_MAIL_API_TOKEN"] = cosmic_mail_api_token
    if primary_mailbox_address is not None:
        overrides["COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"] = primary_mailbox_address
    if internal_llm_api_key is not None:
        overrides["EMAIL_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["EMAIL_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return email_agent_system_env_path(system_env_dir), rendered, rendered_data


def email_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return (
        meaningful_env_value(env_values.get("COSMIC_MAIL_BASE_URL")) is not None
        and meaningful_env_value(env_values.get("COSMIC_MAIL_API_TOKEN")) is not None
    )


def read_email_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = email_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def image_generator_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "image_generator_agent"


def image_generator_agent_repo_env_path() -> Path:
    return image_generator_agent_repo_dir() / "agent.env"


def image_generator_agent_repo_env_example_path() -> Path:
    return image_generator_agent_repo_dir() / "agent.env.example"


def image_generator_agent_system_env_path(
    system_env_dir: Optional[Path] = None,
) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR)
        / "agents"
        / IMAGE_GENERATOR_AGENT_ENV_NAME
    )


def resolve_image_generator_agent_env_source() -> Path:
    repo_env = image_generator_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return image_generator_agent_repo_env_example_path()


def build_image_generator_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_image_generator_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    router_api_key = first_meaningful_value(
        external_env.get("IMAGE_AGENT_ROUTER_API_KEY"),
        external_env.get("OPENAI_API_KEY"),
        existing_env.get("IMAGE_AGENT_ROUTER_API_KEY"),
        existing_env.get("OPENAI_API_KEY"),
        source_data.get("IMAGE_AGENT_ROUTER_API_KEY"),
        source_data.get("OPENAI_API_KEY"),
    )
    router_base_url = first_meaningful_value(
        external_env.get("IMAGE_AGENT_ROUTER_BASE_URL"),
        external_env.get("OPENAI_BASE_URL"),
        existing_env.get("IMAGE_AGENT_ROUTER_BASE_URL"),
        existing_env.get("OPENAI_BASE_URL"),
        source_data.get("IMAGE_AGENT_ROUTER_BASE_URL"),
        source_data.get("OPENAI_BASE_URL"),
        "https://api.openai.com/v1",
    )
    router_model = first_meaningful_value(
        external_env.get("IMAGE_AGENT_ROUTER_MODEL"),
        existing_env.get("IMAGE_AGENT_ROUTER_MODEL"),
        source_data.get("IMAGE_AGENT_ROUTER_MODEL"),
        "gpt-5-mini",
    )
    openai_api_key = first_meaningful_value(
        external_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        external_env.get("OPENAI_API_KEY"),
        existing_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        existing_env.get("OPENAI_API_KEY"),
        source_data.get("IMAGE_AGENT_OPENAI_API_KEY"),
        source_data.get("OPENAI_API_KEY"),
    )
    openai_base_url = first_meaningful_value(
        external_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        external_env.get("OPENAI_BASE_URL"),
        existing_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        existing_env.get("OPENAI_BASE_URL"),
        source_data.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        source_data.get("OPENAI_BASE_URL"),
        "https://api.openai.com/v1",
    )
    openai_model = first_meaningful_value(
        external_env.get("IMAGE_AGENT_OPENAI_MODEL"),
        existing_env.get("IMAGE_AGENT_OPENAI_MODEL"),
        source_data.get("IMAGE_AGENT_OPENAI_MODEL"),
        "gpt-image-1.5",
    )
    xai_api_key = first_meaningful_value(
        external_env.get("IMAGE_AGENT_XAI_API_KEY"),
        external_env.get("XAI_API_KEY"),
        existing_env.get("IMAGE_AGENT_XAI_API_KEY"),
        existing_env.get("XAI_API_KEY"),
        source_data.get("IMAGE_AGENT_XAI_API_KEY"),
        source_data.get("XAI_API_KEY"),
    )
    xai_base_url = first_meaningful_value(
        external_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        existing_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        source_data.get("IMAGE_AGENT_XAI_BASE_URL"),
        "https://api.x.ai/v1",
    )
    xai_model = first_meaningful_value(
        external_env.get("IMAGE_AGENT_XAI_MODEL"),
        existing_env.get("IMAGE_AGENT_XAI_MODEL"),
        source_data.get("IMAGE_AGENT_XAI_MODEL"),
        "grok-imagine-image-pro",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        IMAGE_GENERATOR_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or IMAGE_GENERATOR_AGENT_DEFAULT_INSTANCE_ID,
        "IMAGE_AGENT_ROUTER_BASE_URL": router_base_url or "https://api.openai.com/v1",
        "IMAGE_AGENT_ROUTER_MODEL": router_model or "gpt-5-mini",
        "IMAGE_AGENT_OPENAI_BASE_URL": openai_base_url or "https://api.openai.com/v1",
        "IMAGE_AGENT_OPENAI_MODEL": openai_model or "gpt-image-1.5",
        "IMAGE_AGENT_XAI_BASE_URL": xai_base_url or "https://api.x.ai/v1",
        "IMAGE_AGENT_XAI_MODEL": xai_model or "grok-imagine-image-pro",
    }
    if router_api_key is not None:
        overrides["IMAGE_AGENT_ROUTER_API_KEY"] = router_api_key
    if openai_api_key is not None:
        overrides["IMAGE_AGENT_OPENAI_API_KEY"] = openai_api_key
    if xai_api_key is not None:
        overrides["IMAGE_AGENT_XAI_API_KEY"] = xai_api_key

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return (
        image_generator_agent_system_env_path(system_env_dir),
        rendered,
        rendered_data,
    )


def image_generator_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return (
        meaningful_env_value(env_values.get("IMAGE_AGENT_XAI_API_KEY")) is not None
        or meaningful_env_value(env_values.get("XAI_API_KEY")) is not None
        or meaningful_env_value(env_values.get("IMAGE_AGENT_OPENAI_API_KEY"))
        is not None
        or meaningful_env_value(env_values.get("OPENAI_API_KEY")) is not None
    )


def read_image_generator_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = image_generator_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def calendar_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "calendar_agent"


def calendar_agent_repo_env_path() -> Path:
    return calendar_agent_repo_dir() / "agent.env"


def calendar_agent_repo_env_example_path() -> Path:
    return calendar_agent_repo_dir() / "agent.env.example"


def calendar_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / CALENDAR_AGENT_ENV_NAME
    )


def resolve_calendar_agent_env_source() -> Path:
    repo_env = calendar_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return calendar_agent_repo_env_example_path()


def build_calendar_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_calendar_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(CALENDAR_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(CALENDAR_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    internal_llm_api_key = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("CALENDAR_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("CALENDAR_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("CALENDAR_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("CALENDAR_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )
    internal_llm_model = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_INTERNAL_LLM_MODEL"),
        existing_env.get("CALENDAR_AGENT_INTERNAL_LLM_MODEL"),
        source_data.get("CALENDAR_AGENT_INTERNAL_LLM_MODEL"),
        "gpt-5-mini",
    )
    internal_llm_timeout_sec = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        existing_env.get("CALENDAR_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        source_data.get("CALENDAR_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        "120.0",
    )
    enable_internal_llm = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_ENABLE_INTERNAL_LLM"),
        existing_env.get("CALENDAR_AGENT_ENABLE_INTERNAL_LLM"),
        source_data.get("CALENDAR_AGENT_ENABLE_INTERNAL_LLM"),
        "true",
    )
    use_langgraph = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_USE_LANGGRAPH"),
        existing_env.get("CALENDAR_AGENT_USE_LANGGRAPH"),
        source_data.get("CALENDAR_AGENT_USE_LANGGRAPH"),
        "true",
    )
    max_tool_rounds = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_MAX_TOOL_ROUNDS"),
        existing_env.get("CALENDAR_AGENT_MAX_TOOL_ROUNDS"),
        source_data.get("CALENDAR_AGENT_MAX_TOOL_ROUNDS"),
        "6",
    )
    default_timezone = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_DEFAULT_TIMEZONE"),
        existing_env.get("CALENDAR_AGENT_DEFAULT_TIMEZONE"),
        source_data.get("CALENDAR_AGENT_DEFAULT_TIMEZONE"),
        "America/Chicago",
    )
    working_hour_start = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_WORKING_HOUR_START"),
        existing_env.get("CALENDAR_AGENT_WORKING_HOUR_START"),
        source_data.get("CALENDAR_AGENT_WORKING_HOUR_START"),
        "9",
    )
    working_hour_end = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_WORKING_HOUR_END"),
        existing_env.get("CALENDAR_AGENT_WORKING_HOUR_END"),
        source_data.get("CALENDAR_AGENT_WORKING_HOUR_END"),
        "17",
    )
    default_duration_min = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_DEFAULT_EVENT_DURATION_MIN"),
        existing_env.get("CALENDAR_AGENT_DEFAULT_EVENT_DURATION_MIN"),
        source_data.get("CALENDAR_AGENT_DEFAULT_EVENT_DURATION_MIN"),
        "30",
    )
    buffer_min = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_BUFFER_MIN"),
        existing_env.get("CALENDAR_AGENT_BUFFER_MIN"),
        source_data.get("CALENDAR_AGENT_BUFFER_MIN"),
        "15",
    )
    max_events_per_list = first_meaningful_value(
        external_env.get("CALENDAR_AGENT_MAX_EVENTS_PER_LIST"),
        existing_env.get("CALENDAR_AGENT_MAX_EVENTS_PER_LIST"),
        source_data.get("CALENDAR_AGENT_MAX_EVENTS_PER_LIST"),
        "50",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        CALENDAR_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or CALENDAR_AGENT_DEFAULT_INSTANCE_ID,
        "CALENDAR_AGENT_INTERNAL_LLM_MODEL": internal_llm_model or "gpt-5-mini",
        "CALENDAR_AGENT_INTERNAL_LLM_TIMEOUT_SEC": internal_llm_timeout_sec or "120.0",
        "CALENDAR_AGENT_ENABLE_INTERNAL_LLM": enable_internal_llm or "true",
        "CALENDAR_AGENT_USE_LANGGRAPH": use_langgraph or "true",
        "CALENDAR_AGENT_MAX_TOOL_ROUNDS": max_tool_rounds or "6",
        "CALENDAR_AGENT_DEFAULT_TIMEZONE": default_timezone or "America/Chicago",
        "CALENDAR_AGENT_WORKING_HOUR_START": working_hour_start or "9",
        "CALENDAR_AGENT_WORKING_HOUR_END": working_hour_end or "17",
        "CALENDAR_AGENT_DEFAULT_EVENT_DURATION_MIN": default_duration_min or "30",
        "CALENDAR_AGENT_BUFFER_MIN": buffer_min or "15",
        "CALENDAR_AGENT_MAX_EVENTS_PER_LIST": max_events_per_list or "50",
    }
    if internal_llm_api_key is not None:
        overrides["CALENDAR_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["CALENDAR_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return calendar_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_calendar_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = calendar_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def gmail_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "gmail_agent"


def gmail_agent_repo_env_path() -> Path:
    return gmail_agent_repo_dir() / "agent.env"


def gmail_agent_repo_env_example_path() -> Path:
    return gmail_agent_repo_dir() / "agent.env.example"


def gmail_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    system_env_dir = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
    return system_env_dir / "agents" / GMAIL_AGENT_ENV_NAME


def resolve_gmail_agent_env_source() -> Path:
    repo_env = gmail_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return gmail_agent_repo_env_example_path()


def build_gmail_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_gmail_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    email_existing_env = (existing_env_by_name or {}).get(EMAIL_AGENT_ENV_NAME, {})
    email_external_env = (external_env_by_name or {}).get(EMAIL_AGENT_ENV_NAME, {})
    image_existing_env = (existing_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})
    image_external_env = (external_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})
    slide_existing_env = (existing_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    slide_external_env = (external_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    gateway_existing_env = (existing_env_by_name or {}).get("gateway.env", {})
    gateway_external_env = (external_env_by_name or {}).get("gateway.env", {})
    orchestrator_existing_env = (existing_env_by_name or {}).get("orchestrator.env", {})
    orchestrator_external_env = (external_env_by_name or {}).get("orchestrator.env", {})

    def read_peer_env(env_name: str, *, agent_env: bool = True) -> Dict[str, str]:
        base = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
        path = (base / "agents" / env_name) if agent_env else (base / env_name)
        if not path.exists():
            return {}
        try:
            if is_linux():
                return parse_env_text(read_text_file(path, use_sudo=True))
            return parse_env_text(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    if not email_existing_env:
        email_existing_env = read_peer_env(EMAIL_AGENT_ENV_NAME)
    if not image_existing_env:
        image_existing_env = read_peer_env(IMAGE_GENERATOR_AGENT_ENV_NAME)
    if not slide_existing_env:
        slide_existing_env = read_peer_env(SLIDE_AGENT_ENV_NAME)
    if not gateway_existing_env:
        gateway_existing_env = read_peer_env("gateway.env", agent_env=False)
    if not orchestrator_existing_env:
        orchestrator_existing_env = read_peer_env("orchestrator.env", agent_env=False)

    def pick(key: str, default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            external_env.get(key),
            existing_env.get(key),
            source_data.get(key),
            default,
        )

    internal_llm_api_key = first_meaningful_value(
        external_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        email_external_env.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        email_external_env.get("OPENAI_COMPAT_API_KEY"),
        image_external_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        image_external_env.get("OPENAI_API_KEY"),
        slide_external_env.get("MODEL_API_KEY"),
        slide_external_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_external_env.get("OPENAI_API_KEY"),
        gateway_external_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_external_env.get("OPENAI_API_KEY"),
        orchestrator_external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        email_existing_env.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        email_existing_env.get("OPENAI_COMPAT_API_KEY"),
        image_existing_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        image_existing_env.get("OPENAI_API_KEY"),
        slide_existing_env.get("MODEL_API_KEY"),
        slide_existing_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_existing_env.get("OPENAI_API_KEY"),
        gateway_existing_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_existing_env.get("OPENAI_API_KEY"),
        orchestrator_existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        email_external_env.get("EMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        email_external_env.get("OPENAI_COMPAT_BASE_URL"),
        image_external_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        slide_external_env.get("MODEL_BASE_URL"),
        slide_external_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_external_env.get("OPENAI_BASE_URL"),
        gateway_external_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_external_env.get("OPENAI_BASE_URL"),
        orchestrator_external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        email_existing_env.get("EMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        email_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        image_existing_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        slide_existing_env.get("MODEL_BASE_URL"),
        slide_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_existing_env.get("OPENAI_BASE_URL"),
        gateway_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_existing_env.get("OPENAI_BASE_URL"),
        orchestrator_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
        "https://api.openai.com/v1",
    )

    overrides = {
        "REDIS_URL": pick("REDIS_URL", "redis://127.0.0.1:6379/0")
        or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": pick("GATEWAY_URL", "http://127.0.0.1:8080")
        or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": pick("INSTANCE_ID", GMAIL_AGENT_DEFAULT_INSTANCE_ID)
        or GMAIL_AGENT_DEFAULT_INSTANCE_ID,
        "GMAIL_AGENT_ENABLED": pick("GMAIL_AGENT_ENABLED", "true") or "true",
        "GMAIL_AGENT_INTERNAL_LLM_MODEL": pick(
            "GMAIL_AGENT_INTERNAL_LLM_MODEL", "gpt-5-mini"
        )
        or "gpt-5-mini",
        "GMAIL_AGENT_INTERNAL_LLM_TIMEOUT_SEC": pick(
            "GMAIL_AGENT_INTERNAL_LLM_TIMEOUT_SEC", "90.0"
        )
        or "90.0",
        "GMAIL_AGENT_ENABLE_INTERNAL_LLM": pick(
            "GMAIL_AGENT_ENABLE_INTERNAL_LLM", "true"
        )
        or "true",
        "GMAIL_AGENT_MAX_SEARCH_RESULTS": pick("GMAIL_AGENT_MAX_SEARCH_RESULTS", "10")
        or "10",
        "GMAIL_AGENT_MAX_TRIAGE_MESSAGES": pick(
            "GMAIL_AGENT_MAX_TRIAGE_MESSAGES", "12"
        )
        or "12",
        "GMAIL_AGENT_MAX_THREAD_MESSAGES": pick(
            "GMAIL_AGENT_MAX_THREAD_MESSAGES", "40"
        )
        or "40",
        "GMAIL_AGENT_MAX_BODY_CHARS": pick("GMAIL_AGENT_MAX_BODY_CHARS", "6000")
        or "6000",
        "GMAIL_AGENT_MAX_DIGEST_ITEMS": pick("GMAIL_AGENT_MAX_DIGEST_ITEMS", "6")
        or "6",
        "GMAIL_AGENT_AUTO_PREFILTER_HIGH_CONFIDENCE_NOISE": pick(
            "GMAIL_AGENT_AUTO_PREFILTER_HIGH_CONFIDENCE_NOISE", "true"
        )
        or "true",
        "GMAIL_AGENT_PREFILTER_CONFIDENCE_THRESHOLD": pick(
            "GMAIL_AGENT_PREFILTER_CONFIDENCE_THRESHOLD", "0.92"
        )
        or "0.92",
        "GMAIL_WATCH_TOPIC_NAME": pick("GMAIL_WATCH_TOPIC_NAME", "") or "",
        "GMAIL_WATCH_LABEL_IDS": pick("GMAIL_WATCH_LABEL_IDS", "INBOX") or "INBOX",
        "GMAIL_WEBHOOK_SECRET": first_meaningful_value(
            external_env.get("GMAIL_WEBHOOK_SECRET"),
            gateway_external_env.get("GATEWAY_GMAIL_WEBHOOK_SECRET"),
            gateway_external_env.get("GMAIL_WEBHOOK_SECRET"),
            existing_env.get("GMAIL_WEBHOOK_SECRET"),
            gateway_existing_env.get("GATEWAY_GMAIL_WEBHOOK_SECRET"),
            gateway_existing_env.get("GMAIL_WEBHOOK_SECRET"),
            source_data.get("GMAIL_WEBHOOK_SECRET"),
        )
        or "",
    }
    if internal_llm_api_key is not None:
        overrides["GMAIL_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["GMAIL_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return gmail_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_gmail_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = gmail_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def gmail_agent_is_configured(env_values: Dict[str, str]) -> bool:
    enabled = str(env_values.get("GMAIL_AGENT_ENABLED") or "").strip().lower()
    if enabled in {"1", "true", "yes", "on"}:
        return True
    return meaningful_env_value(env_values.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY")) is not None


def google_docs_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "google_docs_agent"


def google_docs_agent_repo_env_path() -> Path:
    return google_docs_agent_repo_dir() / "agent.env"


def google_docs_agent_repo_env_example_path() -> Path:
    return google_docs_agent_repo_dir() / "agent.env.example"


def google_docs_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    system_env_dir = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
    return system_env_dir / "agents" / GOOGLE_DOCS_AGENT_ENV_NAME


def resolve_google_docs_agent_env_source() -> Path:
    repo_env = google_docs_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return google_docs_agent_repo_env_example_path()


def build_google_docs_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_google_docs_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(GOOGLE_DOCS_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(GOOGLE_DOCS_AGENT_ENV_NAME, {})
    gmail_existing_env = (existing_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    gmail_external_env = (external_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    gateway_existing_env = (existing_env_by_name or {}).get("gateway.env", {})
    gateway_external_env = (external_env_by_name or {}).get("gateway.env", {})
    orchestrator_existing_env = (existing_env_by_name or {}).get("orchestrator.env", {})
    orchestrator_external_env = (external_env_by_name or {}).get("orchestrator.env", {})
    slide_existing_env = (existing_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    slide_external_env = (external_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})

    def pick(key: str, default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            external_env.get(key),
            existing_env.get(key),
            source_data.get(key),
            default,
        )

    internal_llm_api_key = first_meaningful_value(
        external_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        external_env.get("OPENAI_API_KEY"),
        gmail_external_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        gmail_external_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_external_env.get("OPENAI_API_KEY"),
        gateway_external_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_external_env.get("OPENAI_API_KEY"),
        orchestrator_external_env.get("OPENAI_COMPAT_API_KEY"),
        slide_external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("OPENAI_API_KEY"),
        gmail_existing_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        gmail_existing_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_existing_env.get("OPENAI_API_KEY"),
        gateway_existing_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_existing_env.get("OPENAI_API_KEY"),
        orchestrator_existing_env.get("OPENAI_COMPAT_API_KEY"),
        slide_existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("OPENAI_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        gmail_external_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        gmail_external_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_external_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_external_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        gmail_existing_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        gmail_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )

    overrides = {
        "REDIS_URL": pick("REDIS_URL", "redis://127.0.0.1:6379/0")
        or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": pick("GATEWAY_URL", "http://127.0.0.1:8080")
        or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": pick("INSTANCE_ID", GOOGLE_DOCS_AGENT_DEFAULT_INSTANCE_ID)
        or GOOGLE_DOCS_AGENT_DEFAULT_INSTANCE_ID,
        "GOOGLE_DOCS_AGENT_ENABLED": pick("GOOGLE_DOCS_AGENT_ENABLED", "true")
        or "true",
        "GOOGLE_DOCS_AGENT_INTERNAL_LLM_MODEL": pick(
            "GOOGLE_DOCS_AGENT_INTERNAL_LLM_MODEL", "gpt-5-mini"
        )
        or "gpt-5-mini",
        "GOOGLE_DOCS_AGENT_INTERNAL_LLM_TIMEOUT_SEC": pick(
            "GOOGLE_DOCS_AGENT_INTERNAL_LLM_TIMEOUT_SEC", "120.0"
        )
        or "120.0",
        "GOOGLE_DOCS_AGENT_ENABLE_INTERNAL_LLM": pick(
            "GOOGLE_DOCS_AGENT_ENABLE_INTERNAL_LLM", "true"
        )
        or "true",
        "GOOGLE_DOCS_AGENT_REQUEST_TIMEOUT_SEC": pick(
            "GOOGLE_DOCS_AGENT_REQUEST_TIMEOUT_SEC", "30"
        )
        or "30",
        "GOOGLE_DOCS_AGENT_MAX_SEARCH_RESULTS": pick(
            "GOOGLE_DOCS_AGENT_MAX_SEARCH_RESULTS", "10"
        )
        or "10",
        "GOOGLE_DOCS_AGENT_MAX_READ_CHARS": pick(
            "GOOGLE_DOCS_AGENT_MAX_READ_CHARS", "30000"
        )
        or "30000",
        "GOOGLE_DOCS_AGENT_MAX_BLOCKS": pick("GOOGLE_DOCS_AGENT_MAX_BLOCKS", "200")
        or "200",
        "GOOGLE_DOCS_AGENT_MAX_COMMENTS": pick(
            "GOOGLE_DOCS_AGENT_MAX_COMMENTS", "100"
        )
        or "100",
    }
    if internal_llm_api_key is not None:
        overrides["GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url
    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return google_docs_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_google_docs_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = google_docs_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def google_docs_agent_is_configured(env_values: Dict[str, str]) -> bool:
    enabled = str(env_values.get("GOOGLE_DOCS_AGENT_ENABLED") or "true").strip().lower()
    return enabled not in {"0", "false", "no", "off"}


def google_sheets_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "google_sheets_agent"


def google_sheets_agent_repo_env_path() -> Path:
    return google_sheets_agent_repo_dir() / "agent.env"


def google_sheets_agent_repo_env_example_path() -> Path:
    return google_sheets_agent_repo_dir() / "agent.env.example"


def google_sheets_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    system_env_dir = system_env_dir or DEFAULT_SYSTEM_ENV_DIR
    return system_env_dir / "agents" / GOOGLE_SHEETS_AGENT_ENV_NAME


def resolve_google_sheets_agent_env_source() -> Path:
    repo_env = google_sheets_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return google_sheets_agent_repo_env_example_path()


def build_google_sheets_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_google_sheets_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(GOOGLE_SHEETS_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(GOOGLE_SHEETS_AGENT_ENV_NAME, {})
    docs_existing_env = (existing_env_by_name or {}).get(GOOGLE_DOCS_AGENT_ENV_NAME, {})
    docs_external_env = (external_env_by_name or {}).get(GOOGLE_DOCS_AGENT_ENV_NAME, {})
    gmail_existing_env = (existing_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    gmail_external_env = (external_env_by_name or {}).get(GMAIL_AGENT_ENV_NAME, {})
    gateway_existing_env = (existing_env_by_name or {}).get("gateway.env", {})
    gateway_external_env = (external_env_by_name or {}).get("gateway.env", {})
    orchestrator_existing_env = (existing_env_by_name or {}).get("orchestrator.env", {})
    orchestrator_external_env = (external_env_by_name or {}).get("orchestrator.env", {})
    slide_existing_env = (existing_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    slide_external_env = (external_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})

    def pick(key: str, default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            external_env.get(key),
            existing_env.get(key),
            source_data.get(key),
            default,
        )

    internal_llm_api_key = first_meaningful_value(
        external_env.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        external_env.get("OPENAI_API_KEY"),
        docs_external_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"),
        docs_external_env.get("OPENAI_COMPAT_API_KEY"),
        gmail_external_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        gmail_external_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_external_env.get("OPENAI_API_KEY"),
        gateway_external_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_external_env.get("OPENAI_API_KEY"),
        orchestrator_external_env.get("OPENAI_COMPAT_API_KEY"),
        slide_external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("OPENAI_API_KEY"),
        docs_existing_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY"),
        docs_existing_env.get("OPENAI_COMPAT_API_KEY"),
        gmail_existing_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY"),
        gmail_existing_env.get("OPENAI_COMPAT_API_KEY"),
        gateway_existing_env.get("OPENAI_API_KEY"),
        gateway_existing_env.get("OPENAI_COMPAT_API_KEY"),
        orchestrator_existing_env.get("OPENAI_API_KEY"),
        orchestrator_existing_env.get("OPENAI_COMPAT_API_KEY"),
        slide_existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("OPENAI_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        docs_external_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"),
        docs_external_env.get("OPENAI_COMPAT_BASE_URL"),
        gmail_external_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        gmail_external_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_external_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_external_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        docs_existing_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_BASE_URL"),
        docs_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        gmail_existing_env.get("GMAIL_AGENT_INTERNAL_LLM_BASE_URL"),
        gmail_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        gateway_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        orchestrator_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        slide_existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )

    overrides = {
        "REDIS_URL": pick("REDIS_URL", "redis://127.0.0.1:6379/0")
        or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": pick("GATEWAY_URL", "http://127.0.0.1:8080")
        or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": pick("INSTANCE_ID", GOOGLE_SHEETS_AGENT_DEFAULT_INSTANCE_ID)
        or GOOGLE_SHEETS_AGENT_DEFAULT_INSTANCE_ID,
        "GOOGLE_SHEETS_AGENT_ENABLED": pick("GOOGLE_SHEETS_AGENT_ENABLED", "true")
        or "true",
        "GOOGLE_SHEETS_AGENT_INTERNAL_LLM_MODEL": pick(
            "GOOGLE_SHEETS_AGENT_INTERNAL_LLM_MODEL", "gpt-5-mini"
        )
        or "gpt-5-mini",
        "GOOGLE_SHEETS_AGENT_INTERNAL_LLM_TIMEOUT_SEC": pick(
            "GOOGLE_SHEETS_AGENT_INTERNAL_LLM_TIMEOUT_SEC", "120.0"
        )
        or "120.0",
        "GOOGLE_SHEETS_AGENT_ENABLE_INTERNAL_LLM": pick(
            "GOOGLE_SHEETS_AGENT_ENABLE_INTERNAL_LLM", "true"
        )
        or "true",
        "GOOGLE_SHEETS_AGENT_REQUEST_TIMEOUT_SEC": pick(
            "GOOGLE_SHEETS_AGENT_REQUEST_TIMEOUT_SEC", "30"
        )
        or "30",
        "GOOGLE_SHEETS_AGENT_MAX_SEARCH_RESULTS": pick(
            "GOOGLE_SHEETS_AGENT_MAX_SEARCH_RESULTS", "10"
        )
        or "10",
        "GOOGLE_SHEETS_AGENT_MAX_READ_CELLS": pick(
            "GOOGLE_SHEETS_AGENT_MAX_READ_CELLS", "5000"
        )
        or "5000",
        "GOOGLE_SHEETS_AGENT_MAX_WRITE_CELLS": pick(
            "GOOGLE_SHEETS_AGENT_MAX_WRITE_CELLS", "20000"
        )
        or "20000",
    }
    if internal_llm_api_key is not None:
        overrides["GOOGLE_SHEETS_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["GOOGLE_SHEETS_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url
    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return google_sheets_agent_system_env_path(system_env_dir), rendered, rendered_data


def read_google_sheets_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = google_sheets_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def google_sheets_agent_is_configured(env_values: Dict[str, str]) -> bool:
    enabled = str(env_values.get("GOOGLE_SHEETS_AGENT_ENABLED") or "true").strip().lower()
    return enabled not in {"0", "false", "no", "off"}


def diagram_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "diagram_agent"


def diagram_agent_repo_env_path() -> Path:
    return diagram_agent_repo_dir() / "agent.env"


def diagram_agent_repo_env_example_path() -> Path:
    return diagram_agent_repo_dir() / "agent.env.example"


def diagram_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / DIAGRAM_AGENT_ENV_NAME
    )


def resolve_diagram_agent_env_source() -> Path:
    repo_env = diagram_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return diagram_agent_repo_env_example_path()


def build_diagram_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_diagram_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(DIAGRAM_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(DIAGRAM_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    internal_llm_api_key = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("DIAGRAM_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("DIAGRAM_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("DIAGRAM_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("DIAGRAM_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
    )
    internal_llm_model = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_INTERNAL_LLM_MODEL"),
        existing_env.get("DIAGRAM_AGENT_INTERNAL_LLM_MODEL"),
        source_data.get("DIAGRAM_AGENT_INTERNAL_LLM_MODEL"),
        "gpt-5-mini",
    )
    internal_llm_timeout_sec = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        existing_env.get("DIAGRAM_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        source_data.get("DIAGRAM_AGENT_INTERNAL_LLM_TIMEOUT_SEC"),
        "120.0",
    )
    enable_internal_llm = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_ENABLE_INTERNAL_LLM"),
        existing_env.get("DIAGRAM_AGENT_ENABLE_INTERNAL_LLM"),
        source_data.get("DIAGRAM_AGENT_ENABLE_INTERNAL_LLM"),
        "true",
    )
    use_langgraph = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_USE_LANGGRAPH"),
        existing_env.get("DIAGRAM_AGENT_USE_LANGGRAPH"),
        source_data.get("DIAGRAM_AGENT_USE_LANGGRAPH"),
        "true",
    )
    max_tool_rounds = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MAX_TOOL_ROUNDS"),
        existing_env.get("DIAGRAM_AGENT_MAX_TOOL_ROUNDS"),
        source_data.get("DIAGRAM_AGENT_MAX_TOOL_ROUNDS"),
        "6",
    )
    mmdc_path = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MMDC_PATH"),
        existing_env.get("DIAGRAM_AGENT_MMDC_PATH"),
        source_data.get("DIAGRAM_AGENT_MMDC_PATH"),
        "mmdc",
    )
    d2_path = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_D2_PATH"),
        existing_env.get("DIAGRAM_AGENT_D2_PATH"),
        source_data.get("DIAGRAM_AGENT_D2_PATH"),
        "d2",
    )
    puppeteer_cache_dir = first_meaningful_value(
        external_env.get("PUPPETEER_CACHE_DIR"),
        existing_env.get("PUPPETEER_CACHE_DIR"),
        source_data.get("PUPPETEER_CACHE_DIR"),
        str(DEFAULT_DIAGRAM_PUPPETEER_CACHE_DIR),
    )
    default_format = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_DEFAULT_FORMAT"),
        existing_env.get("DIAGRAM_AGENT_DEFAULT_FORMAT"),
        source_data.get("DIAGRAM_AGENT_DEFAULT_FORMAT"),
        "svg",
    )
    default_theme = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_DEFAULT_THEME"),
        existing_env.get("DIAGRAM_AGENT_DEFAULT_THEME"),
        source_data.get("DIAGRAM_AGENT_DEFAULT_THEME"),
        "default",
    )
    mermaid_bg = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MERMAID_BG"),
        existing_env.get("DIAGRAM_AGENT_MERMAID_BG"),
        source_data.get("DIAGRAM_AGENT_MERMAID_BG"),
        "white",
    )
    mermaid_disable_sandbox = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MERMAID_DISABLE_SANDBOX"),
        existing_env.get("DIAGRAM_AGENT_MERMAID_DISABLE_SANDBOX"),
        source_data.get("DIAGRAM_AGENT_MERMAID_DISABLE_SANDBOX"),
        "true",
    )
    d2_sketch = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_D2_SKETCH"),
        existing_env.get("DIAGRAM_AGENT_D2_SKETCH"),
        source_data.get("DIAGRAM_AGENT_D2_SKETCH"),
        "false",
    )
    d2_pad = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_D2_PAD"),
        existing_env.get("DIAGRAM_AGENT_D2_PAD"),
        source_data.get("DIAGRAM_AGENT_D2_PAD"),
        "100",
    )
    max_width = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MAX_WIDTH_PX"),
        existing_env.get("DIAGRAM_AGENT_MAX_WIDTH_PX"),
        source_data.get("DIAGRAM_AGENT_MAX_WIDTH_PX"),
        "2400",
    )
    max_height = first_meaningful_value(
        external_env.get("DIAGRAM_AGENT_MAX_HEIGHT_PX"),
        existing_env.get("DIAGRAM_AGENT_MAX_HEIGHT_PX"),
        source_data.get("DIAGRAM_AGENT_MAX_HEIGHT_PX"),
        "1600",
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        DIAGRAM_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or DIAGRAM_AGENT_DEFAULT_INSTANCE_ID,
        "DIAGRAM_AGENT_INTERNAL_LLM_MODEL": internal_llm_model or "gpt-5-mini",
        "DIAGRAM_AGENT_INTERNAL_LLM_TIMEOUT_SEC": internal_llm_timeout_sec or "120.0",
        "DIAGRAM_AGENT_ENABLE_INTERNAL_LLM": enable_internal_llm or "true",
        "DIAGRAM_AGENT_USE_LANGGRAPH": use_langgraph or "true",
        "DIAGRAM_AGENT_MAX_TOOL_ROUNDS": max_tool_rounds or "6",
        "DIAGRAM_AGENT_MMDC_PATH": mmdc_path or "mmdc",
        "DIAGRAM_AGENT_D2_PATH": d2_path or "d2",
        "PUPPETEER_CACHE_DIR": puppeteer_cache_dir
        or str(DEFAULT_DIAGRAM_PUPPETEER_CACHE_DIR),
        "DIAGRAM_AGENT_DEFAULT_FORMAT": default_format or "svg",
        "DIAGRAM_AGENT_DEFAULT_THEME": default_theme or "default",
        "DIAGRAM_AGENT_MERMAID_BG": mermaid_bg or "white",
        "DIAGRAM_AGENT_MERMAID_DISABLE_SANDBOX": mermaid_disable_sandbox or "true",
        "DIAGRAM_AGENT_D2_SKETCH": d2_sketch or "false",
        "DIAGRAM_AGENT_D2_PAD": d2_pad or "100",
        "DIAGRAM_AGENT_MAX_WIDTH_PX": max_width or "2400",
        "DIAGRAM_AGENT_MAX_HEIGHT_PX": max_height or "1600",
    }
    if internal_llm_api_key is not None:
        overrides["DIAGRAM_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["DIAGRAM_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return diagram_agent_system_env_path(system_env_dir), rendered, rendered_data


def diagram_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return (
        meaningful_env_value(env_values.get("DIAGRAM_AGENT_INTERNAL_LLM_API_KEY")) is not None
        or meaningful_env_value(env_values.get("OPENAI_COMPAT_API_KEY")) is not None
    )


def read_diagram_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = diagram_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def map_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "map_agent"


def map_agent_repo_env_path() -> Path:
    return map_agent_repo_dir() / "agent.env"


def map_agent_repo_env_example_path() -> Path:
    return map_agent_repo_dir() / "agent.env.example"


def map_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / MAP_AGENT_ENV_NAME


def resolve_map_agent_env_source() -> Path:
    repo_env = map_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return map_agent_repo_env_example_path()


def build_map_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_map_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(MAP_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(MAP_AGENT_ENV_NAME, {})
    docs_parser_existing_env = (existing_env_by_name or {}).get(DOCS_PARSER_AGENT_ENV_NAME, {})
    docs_parser_external_env = (external_env_by_name or {}).get(DOCS_PARSER_AGENT_ENV_NAME, {})
    image_existing_env = (existing_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})
    image_external_env = (external_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    internal_llm_api_key = first_meaningful_value(
        external_env.get("MAP_AGENT_INTERNAL_LLM_API_KEY"),
        external_env.get("OPENAI_COMPAT_API_KEY"),
        external_env.get("OPENAI_API_KEY"),
        docs_parser_external_env.get("OPENAI_API_KEY"),
        image_external_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        existing_env.get("MAP_AGENT_INTERNAL_LLM_API_KEY"),
        existing_env.get("OPENAI_COMPAT_API_KEY"),
        existing_env.get("OPENAI_API_KEY"),
        docs_parser_existing_env.get("OPENAI_API_KEY"),
        image_existing_env.get("IMAGE_AGENT_OPENAI_API_KEY"),
        source_data.get("MAP_AGENT_INTERNAL_LLM_API_KEY"),
        source_data.get("OPENAI_COMPAT_API_KEY"),
        source_data.get("OPENAI_API_KEY"),
    )
    internal_llm_base_url = first_meaningful_value(
        external_env.get("MAP_AGENT_INTERNAL_LLM_BASE_URL"),
        external_env.get("OPENAI_COMPAT_BASE_URL"),
        external_env.get("OPENAI_BASE_URL"),
        image_external_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        existing_env.get("MAP_AGENT_INTERNAL_LLM_BASE_URL"),
        existing_env.get("OPENAI_COMPAT_BASE_URL"),
        existing_env.get("OPENAI_BASE_URL"),
        image_existing_env.get("IMAGE_AGENT_OPENAI_BASE_URL"),
        source_data.get("MAP_AGENT_INTERNAL_LLM_BASE_URL"),
        source_data.get("OPENAI_COMPAT_BASE_URL"),
        source_data.get("OPENAI_BASE_URL"),
    )
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        MAP_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or MAP_AGENT_DEFAULT_INSTANCE_ID,
        "MAP_AGENT_INTERNAL_LLM_MODEL": first_meaningful_value(
            external_env.get("MAP_AGENT_INTERNAL_LLM_MODEL"),
            existing_env.get("MAP_AGENT_INTERNAL_LLM_MODEL"),
            source_data.get("MAP_AGENT_INTERNAL_LLM_MODEL"),
            "gpt-5-mini",
        )
        or "gpt-5-mini",
        "MAP_AGENT_ENABLE_INTERNAL_LLM": first_meaningful_value(
            external_env.get("MAP_AGENT_ENABLE_INTERNAL_LLM"),
            existing_env.get("MAP_AGENT_ENABLE_INTERNAL_LLM"),
            source_data.get("MAP_AGENT_ENABLE_INTERNAL_LLM"),
            "true",
        )
        or "true",
    }
    if internal_llm_api_key is not None:
        overrides["MAP_AGENT_INTERNAL_LLM_API_KEY"] = internal_llm_api_key
    if internal_llm_base_url is not None:
        overrides["MAP_AGENT_INTERNAL_LLM_BASE_URL"] = internal_llm_base_url
    elif internal_llm_api_key is not None:
        overrides["MAP_AGENT_INTERNAL_LLM_BASE_URL"] = "https://api.openai.com/v1"

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return map_agent_system_env_path(system_env_dir), rendered, rendered_data


def map_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return meaningful_env_value(env_values.get("AGENT_SECRET")) is not None


def read_map_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = map_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def slide_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "slide_agent"


def slide_agent_repo_env_path() -> Path:
    return slide_agent_repo_dir() / "agent.env"


def slide_agent_repo_env_example_path() -> Path:
    return slide_agent_repo_dir() / "agent.env.example"


def slide_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (
        (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / SLIDE_AGENT_ENV_NAME
    )


def resolve_slide_agent_env_source() -> Path:
    repo_env = slide_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return slide_agent_repo_env_example_path()


def build_slide_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_slide_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(SLIDE_AGENT_ENV_NAME, {})
    firecrawl_external_env = (external_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})
    image_external_env = (external_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})
    firecrawl_existing_env = (existing_env_by_name or {}).get(FIRECRAWL_AGENT_ENV_NAME, {})
    image_existing_env = (existing_env_by_name or {}).get(IMAGE_GENERATOR_AGENT_ENV_NAME, {})

    def read_peer_env(env_name: str) -> Dict[str, str]:
        path = ((system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / env_name)
        if not path.exists():
            return {}
        try:
            if is_linux():
                return parse_env_text(read_text_file(path, use_sudo=True))
            return parse_env_text(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    if not firecrawl_existing_env:
        firecrawl_existing_env = read_peer_env(FIRECRAWL_AGENT_ENV_NAME)
    if not image_existing_env:
        image_existing_env = read_peer_env(IMAGE_GENERATOR_AGENT_ENV_NAME)

    redis_url = first_meaningful_value(
        external_env.get("REDIS_URL"),
        existing_env.get("REDIS_URL"),
        source_data.get("REDIS_URL"),
        "redis://127.0.0.1:6379/0",
    )
    gateway_url = first_meaningful_value(
        external_env.get("GATEWAY_URL"),
        existing_env.get("GATEWAY_URL"),
        source_data.get("GATEWAY_URL"),
        "http://127.0.0.1:8080",
    )
    def pick_env(names: Sequence[str], default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            *(external_env.get(name) for name in names),
            *(existing_env.get(name) for name in names),
            *(source_data.get(name) for name in names),
            default,
        )

    internal_llm_api_key = pick_env(
        (
            "MODEL_API_KEY",
            "SLIDE_AGENT_FIREWORKS_API_KEY",
            "FIREWORKS_API_KEY",
            "OPENAI_COMPAT_API_KEY",
            "OPENROUTER_API_KEY",
        )
    )
    internal_llm_base_url = pick_env(
        (
            "MODEL_BASE_URL",
            "SLIDE_AGENT_FIREWORKS_BASE_URL",
            "FIREWORKS_BASE_URL",
            "OPENAI_COMPAT_BASE_URL",
            "OPENROUTER_BASE_URL",
        ),
        "https://api.fireworks.ai/inference/v1",
    )
    internal_llm_model = pick_env(
        ("MODEL_NAME", "SLIDE_AGENT_FIREWORKS_MODEL", "FIREWORKS_KIMI_MODEL"),
        "accounts/fireworks/models/qwen3p6-plus",
    )
    model_timeout_sec = pick_env(
        ("MODEL_TIMEOUT_SEC", "SLIDE_AGENT_FIREWORKS_TIMEOUT_SEC"),
        "300",
    )
    model_http_retries = pick_env(("MODEL_HTTP_RETRIES",), "3")
    model_max_tokens = pick_env(("MODEL_MAX_TOKENS",), "16384")
    html_model_max_tokens = pick_env(("HTML_MODEL_MAX_TOKENS",), "4096")
    vision_model_name = pick_env(("VISION_MODEL_NAME",), internal_llm_model)
    libreoffice_path = pick_env(
        ("LIBREOFFICE_PATH", "SLIDE_AGENT_LIBREOFFICE_PATH"),
        "soffice",
    )
    pdftoppm_path = pick_env(
        ("PDFTOPPM_PATH", "SLIDE_AGENT_PDFTOPPM_PATH"),
        "pdftoppm",
    )
    max_slides = pick_env(("SLIDE_AGENT_MAX_SLIDES", "MAX_SLIDES"), "50")
    max_slides_per_deck = pick_env(("SLIDE_AGENT_MAX_SLIDES_PER_DECK",), "50")
    validate_outputs = pick_env(("SLIDE_AGENT_VALIDATE_OUTPUTS",), "true")
    force_catalog_default = pick_env(("SLIDE_AGENT_FORCE_CATALOG_DEFAULT",), "false")
    catalog_parallelism = pick_env(("CATALOG_PARALLELISM",), "5")
    builder_parallelism = pick_env(("BUILDER_PARALLELISM",), "2")
    builder_max_repair_rounds = pick_env(("BUILDER_MAX_REPAIR_ROUNDS",), "2")
    html_max_repair_rounds = pick_env(("HTML_MAX_REPAIR_ROUNDS",), "1")
    html_render_timeout_ms = pick_env(("HTML_RENDER_TIMEOUT_MS",), "45000")
    html_viewport_width = pick_env(("HTML_VIEWPORT_WIDTH",), "1440")
    html_viewport_height = pick_env(("HTML_VIEWPORT_HEIGHT",), "900")
    html_device_scale = pick_env(("HTML_DEVICE_SCALE",), "1.5")
    docs_parser_agent_id = pick_env(
        ("SLIDE_AGENT_DOCS_PARSER_AGENT_ID",),
        DOCS_PARSER_AGENT_ID,
    )
    default_workflow = pick_env(("SLIDE_AGENT_DEFAULT_WORKFLOW",), "")
    pexels_api_key = pick_env(("PEXELS_API_KEY",), "")
    assets_cache_dir = pick_env(("ASSETS_CACHE_DIR",), "assets/cache")
    firecrawl_api_key = first_meaningful_value(
        external_env.get("FIRECRAWL_API_KEY"),
        firecrawl_external_env.get("FIRECRAWL_API_KEY"),
        existing_env.get("FIRECRAWL_API_KEY"),
        firecrawl_existing_env.get("FIRECRAWL_API_KEY"),
        source_data.get("FIRECRAWL_API_KEY"),
    )
    firecrawl_api_base_url = first_meaningful_value(
        external_env.get("FIRECRAWL_API_BASE_URL"),
        firecrawl_external_env.get("FIRECRAWL_API_BASE_URL"),
        existing_env.get("FIRECRAWL_API_BASE_URL"),
        firecrawl_existing_env.get("FIRECRAWL_API_BASE_URL"),
        source_data.get("FIRECRAWL_API_BASE_URL"),
        "https://api.firecrawl.dev",
    )
    firecrawl_request_timeout_sec = first_meaningful_value(
        external_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        firecrawl_external_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        existing_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        source_data.get("FIRECRAWL_REQUEST_TIMEOUT_SEC"),
        "120",
    )
    firecrawl_extract_poll_interval_sec = first_meaningful_value(
        external_env.get("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC"),
        firecrawl_external_env.get("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC"),
        existing_env.get("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC"),
        source_data.get("FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC"),
        "2",
    )
    firecrawl_extract_max_wait_sec = first_meaningful_value(
        external_env.get("FIRECRAWL_EXTRACT_MAX_WAIT_SEC"),
        firecrawl_external_env.get("FIRECRAWL_EXTRACT_MAX_WAIT_SEC"),
        existing_env.get("FIRECRAWL_EXTRACT_MAX_WAIT_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_EXTRACT_MAX_WAIT_SEC"),
        source_data.get("FIRECRAWL_EXTRACT_MAX_WAIT_SEC"),
        "120",
    )
    firecrawl_agent_poll_interval_sec = first_meaningful_value(
        external_env.get("FIRECRAWL_AGENT_POLL_INTERVAL_SEC"),
        firecrawl_external_env.get("FIRECRAWL_AGENT_POLL_INTERVAL_SEC"),
        existing_env.get("FIRECRAWL_AGENT_POLL_INTERVAL_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_AGENT_POLL_INTERVAL_SEC"),
        source_data.get("FIRECRAWL_AGENT_POLL_INTERVAL_SEC"),
        "3",
    )
    firecrawl_agent_max_wait_sec = first_meaningful_value(
        external_env.get("FIRECRAWL_AGENT_MAX_WAIT_SEC"),
        firecrawl_external_env.get("FIRECRAWL_AGENT_MAX_WAIT_SEC"),
        existing_env.get("FIRECRAWL_AGENT_MAX_WAIT_SEC"),
        firecrawl_existing_env.get("FIRECRAWL_AGENT_MAX_WAIT_SEC"),
        source_data.get("FIRECRAWL_AGENT_MAX_WAIT_SEC"),
        "240",
    )
    xai_api_key = first_meaningful_value(
        external_env.get("XAI_API_KEY"),
        external_env.get("IMAGE_AGENT_XAI_API_KEY"),
        image_external_env.get("IMAGE_AGENT_XAI_API_KEY"),
        image_external_env.get("XAI_API_KEY"),
        existing_env.get("XAI_API_KEY"),
        existing_env.get("IMAGE_AGENT_XAI_API_KEY"),
        image_existing_env.get("IMAGE_AGENT_XAI_API_KEY"),
        image_existing_env.get("XAI_API_KEY"),
        source_data.get("XAI_API_KEY"),
        source_data.get("IMAGE_AGENT_XAI_API_KEY"),
    )
    xai_base_url = first_meaningful_value(
        external_env.get("XAI_BASE_URL"),
        external_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        image_external_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        existing_env.get("XAI_BASE_URL"),
        existing_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        image_existing_env.get("IMAGE_AGENT_XAI_BASE_URL"),
        image_existing_env.get("XAI_BASE_URL"),
        source_data.get("XAI_BASE_URL"),
        source_data.get("IMAGE_AGENT_XAI_BASE_URL"),
        "https://api.x.ai/v1",
    )
    xai_model = first_meaningful_value(
        external_env.get("XAI_MODEL"),
        external_env.get("IMAGE_AGENT_XAI_MODEL"),
        image_external_env.get("IMAGE_AGENT_XAI_MODEL"),
        existing_env.get("XAI_MODEL"),
        existing_env.get("IMAGE_AGENT_XAI_MODEL"),
        image_existing_env.get("IMAGE_AGENT_XAI_MODEL"),
        image_existing_env.get("XAI_MODEL"),
        source_data.get("XAI_MODEL"),
        source_data.get("IMAGE_AGENT_XAI_MODEL"),
        "grok-imagine-image-pro",
    )
    xai_timeout_sec = first_meaningful_value(
        external_env.get("XAI_TIMEOUT_SEC"),
        external_env.get("IMAGE_AGENT_XAI_TIMEOUT_SEC"),
        image_external_env.get("IMAGE_AGENT_XAI_TIMEOUT_SEC"),
        existing_env.get("XAI_TIMEOUT_SEC"),
        existing_env.get("IMAGE_AGENT_XAI_TIMEOUT_SEC"),
        image_existing_env.get("IMAGE_AGENT_XAI_TIMEOUT_SEC"),
        image_existing_env.get("XAI_TIMEOUT_SEC"),
        source_data.get("XAI_TIMEOUT_SEC"),
        source_data.get("IMAGE_AGENT_XAI_TIMEOUT_SEC"),
        "180",
    )
    image_agent_default_size = first_meaningful_value(
        external_env.get("IMAGE_AGENT_DEFAULT_SIZE"),
        image_external_env.get("IMAGE_AGENT_DEFAULT_SIZE"),
        existing_env.get("IMAGE_AGENT_DEFAULT_SIZE"),
        image_existing_env.get("IMAGE_AGENT_DEFAULT_SIZE"),
        source_data.get("IMAGE_AGENT_DEFAULT_SIZE"),
        "1536x1024",
    )
    image_agent_default_quality = first_meaningful_value(
        external_env.get("IMAGE_AGENT_DEFAULT_QUALITY"),
        image_external_env.get("IMAGE_AGENT_DEFAULT_QUALITY"),
        existing_env.get("IMAGE_AGENT_DEFAULT_QUALITY"),
        image_existing_env.get("IMAGE_AGENT_DEFAULT_QUALITY"),
        source_data.get("IMAGE_AGENT_DEFAULT_QUALITY"),
        "high",
    )
    image_agent_max_images_per_request = first_meaningful_value(
        external_env.get("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST"),
        image_external_env.get("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST"),
        existing_env.get("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST"),
        image_existing_env.get("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST"),
        source_data.get("IMAGE_AGENT_MAX_IMAGES_PER_REQUEST"),
        "4",
    )
    image_agent_max_prompt_chars = first_meaningful_value(
        external_env.get("IMAGE_AGENT_MAX_PROMPT_CHARS"),
        image_external_env.get("IMAGE_AGENT_MAX_PROMPT_CHARS"),
        existing_env.get("IMAGE_AGENT_MAX_PROMPT_CHARS"),
        image_existing_env.get("IMAGE_AGENT_MAX_PROMPT_CHARS"),
        source_data.get("IMAGE_AGENT_MAX_PROMPT_CHARS"),
        "6000",
    )
    enable_python_sandbox_tool = pick_env(("ENABLE_PYTHON_SANDBOX_TOOL",), "true")
    python_sandbox_timeout_sec = pick_env(("PYTHON_SANDBOX_TIMEOUT_SEC",), "25")
    python_sandbox_max_files = pick_env(("PYTHON_SANDBOX_MAX_FILES",), "8")
    python_sandbox_max_bytes_per_file = pick_env(("PYTHON_SANDBOX_MAX_BYTES_PER_FILE",), "10000000")
    python_sandbox_max_script_bytes = pick_env(("PYTHON_SANDBOX_MAX_SCRIPT_BYTES",), "256000")
    python_sandbox_allow_network = pick_env(("PYTHON_SANDBOX_ALLOW_NETWORK",), "false")
    python_sandbox_allow_pip = pick_env(("PYTHON_SANDBOX_ALLOW_PIP",), "false")
    python_sandbox_pip_timeout_sec = pick_env(("PYTHON_SANDBOX_PIP_TIMEOUT_SEC",), "120")
    python_sandbox_venv_cache_root = pick_env(("PYTHON_SANDBOX_VENV_CACHE_ROOT",), "")
    instance_id = first_meaningful_value(
        external_env.get("INSTANCE_ID"),
        existing_env.get("INSTANCE_ID"),
        source_data.get("INSTANCE_ID"),
        SLIDE_AGENT_DEFAULT_INSTANCE_ID,
    )

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or SLIDE_AGENT_DEFAULT_INSTANCE_ID,
        "SLIDE_AGENT_FIREWORKS_BASE_URL": internal_llm_base_url
        or "https://api.fireworks.ai/inference/v1",
        "SLIDE_AGENT_FIREWORKS_MODEL": internal_llm_model
        or "accounts/fireworks/models/qwen3p6-plus",
        "VISION_MODEL_NAME": vision_model_name
        or internal_llm_model
        or "accounts/fireworks/models/qwen3p6-plus",
        "MODEL_TIMEOUT_SEC": model_timeout_sec or "300",
        "MODEL_HTTP_RETRIES": model_http_retries or "3",
        "MODEL_MAX_TOKENS": model_max_tokens or "16384",
        "HTML_MODEL_MAX_TOKENS": html_model_max_tokens or "4096",
        "SLIDE_AGENT_LIBREOFFICE_PATH": libreoffice_path or "soffice",
        "SLIDE_AGENT_PDFTOPPM_PATH": pdftoppm_path or "pdftoppm",
        "SLIDE_AGENT_MAX_SLIDES": max_slides or "50",
        "SLIDE_AGENT_MAX_SLIDES_PER_DECK": max_slides_per_deck or "50",
        "SLIDE_AGENT_VALIDATE_OUTPUTS": validate_outputs or "true",
        "SLIDE_AGENT_FORCE_CATALOG_DEFAULT": force_catalog_default or "false",
        "CATALOG_PARALLELISM": catalog_parallelism or "5",
        "BUILDER_PARALLELISM": builder_parallelism or "2",
        "BUILDER_MAX_REPAIR_ROUNDS": builder_max_repair_rounds or "2",
        "HTML_MAX_REPAIR_ROUNDS": html_max_repair_rounds or "1",
        "HTML_RENDER_TIMEOUT_MS": html_render_timeout_ms or "45000",
        "HTML_VIEWPORT_WIDTH": html_viewport_width or "1440",
        "HTML_VIEWPORT_HEIGHT": html_viewport_height or "900",
        "HTML_DEVICE_SCALE": html_device_scale or "1.5",
        "SLIDE_AGENT_DOCS_PARSER_AGENT_ID": docs_parser_agent_id
        or DOCS_PARSER_AGENT_ID,
        "SLIDE_AGENT_DEFAULT_WORKFLOW": default_workflow or "",
        "ASSETS_CACHE_DIR": assets_cache_dir or "assets/cache",
        "FIRECRAWL_API_BASE_URL": firecrawl_api_base_url or "https://api.firecrawl.dev",
        "FIRECRAWL_REQUEST_TIMEOUT_SEC": firecrawl_request_timeout_sec or "120",
        "FIRECRAWL_EXTRACT_POLL_INTERVAL_SEC": firecrawl_extract_poll_interval_sec or "2",
        "FIRECRAWL_EXTRACT_MAX_WAIT_SEC": firecrawl_extract_max_wait_sec or "120",
        "FIRECRAWL_AGENT_POLL_INTERVAL_SEC": firecrawl_agent_poll_interval_sec or "3",
        "FIRECRAWL_AGENT_MAX_WAIT_SEC": firecrawl_agent_max_wait_sec or "240",
        "XAI_BASE_URL": xai_base_url or "https://api.x.ai/v1",
        "XAI_MODEL": xai_model or "grok-imagine-image-pro",
        "XAI_TIMEOUT_SEC": xai_timeout_sec or "180",
        "IMAGE_AGENT_DEFAULT_SIZE": image_agent_default_size or "1536x1024",
        "IMAGE_AGENT_DEFAULT_QUALITY": image_agent_default_quality or "high",
        "IMAGE_AGENT_MAX_IMAGES_PER_REQUEST": image_agent_max_images_per_request or "4",
        "IMAGE_AGENT_MAX_PROMPT_CHARS": image_agent_max_prompt_chars or "6000",
        "ENABLE_PYTHON_SANDBOX_TOOL": enable_python_sandbox_tool or "true",
        "PYTHON_SANDBOX_TIMEOUT_SEC": python_sandbox_timeout_sec or "25",
        "PYTHON_SANDBOX_MAX_FILES": python_sandbox_max_files or "8",
        "PYTHON_SANDBOX_MAX_BYTES_PER_FILE": python_sandbox_max_bytes_per_file
        or "10000000",
        "PYTHON_SANDBOX_MAX_SCRIPT_BYTES": python_sandbox_max_script_bytes
        or "256000",
        "PYTHON_SANDBOX_ALLOW_NETWORK": python_sandbox_allow_network or "false",
        "PYTHON_SANDBOX_ALLOW_PIP": python_sandbox_allow_pip or "false",
        "PYTHON_SANDBOX_PIP_TIMEOUT_SEC": python_sandbox_pip_timeout_sec or "120",
        "PYTHON_SANDBOX_VENV_CACHE_ROOT": python_sandbox_venv_cache_root or "",
    }
    if internal_llm_api_key is not None:
        overrides["SLIDE_AGENT_FIREWORKS_API_KEY"] = internal_llm_api_key
    if pexels_api_key is not None:
        overrides["PEXELS_API_KEY"] = pexels_api_key
    if firecrawl_api_key is not None:
        overrides["FIRECRAWL_API_KEY"] = firecrawl_api_key
    if xai_api_key is not None:
        overrides["XAI_API_KEY"] = xai_api_key

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return slide_agent_system_env_path(system_env_dir), rendered, rendered_data


def slide_agent_is_configured(env_values: Dict[str, str]) -> bool:
    return (
        meaningful_env_value(env_values.get("SLIDE_AGENT_FIREWORKS_API_KEY")) is not None
        or meaningful_env_value(env_values.get("FIREWORKS_API_KEY")) is not None
        or meaningful_env_value(env_values.get("OPENAI_COMPAT_API_KEY")) is not None
    )


def read_slide_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = slide_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


def alpha_agent_repo_dir() -> Path:
    return BACKEND_ROOT / "agents" / "alpha_agent"


def alpha_agent_repo_env_path() -> Path:
    return alpha_agent_repo_dir() / "agent.env"


def alpha_agent_repo_env_example_path() -> Path:
    return alpha_agent_repo_dir() / "agent.env.example"


def alpha_agent_system_env_path(system_env_dir: Optional[Path] = None) -> Path:
    return (system_env_dir or DEFAULT_SYSTEM_ENV_DIR) / "agents" / ALPHA_AGENT_ENV_NAME


def resolve_alpha_agent_env_source() -> Path:
    repo_env = alpha_agent_repo_env_path()
    if repo_env.exists():
        return repo_env
    return alpha_agent_repo_env_example_path()


def build_alpha_agent_env_rendered(
    *,
    signing_secret: str,
    shared_internal_token: str,
    system_env_dir: Optional[Path] = None,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    gateway_public_host: Optional[str] = None,
) -> Tuple[Path, str, Dict[str, str]]:
    source_path = resolve_alpha_agent_env_source()
    source_raw = source_path.read_text(encoding="utf-8")
    source_data = parse_env_text(source_raw)
    existing_env = (existing_env_by_name or {}).get(ALPHA_AGENT_ENV_NAME, {})
    external_env = (external_env_by_name or {}).get(ALPHA_AGENT_ENV_NAME, {})

    def pick_env(name: str, default: Optional[str] = None) -> Optional[str]:
        return first_meaningful_value(
            external_env.get(name),
            existing_env.get(name),
            source_data.get(name),
            default,
        )

    redis_url = pick_env("REDIS_URL", "redis://127.0.0.1:6379/0")
    gateway_url = pick_env("GATEWAY_URL", "http://127.0.0.1:8080")
    orchestrator_url = pick_env("ORCHESTRATOR_URL", "http://127.0.0.1:8743")
    instance_id = pick_env("INSTANCE_ID", ALPHA_AGENT_DEFAULT_INSTANCE_ID)

    # GATEWAY_PUBLIC_HOST is the user-facing FQDN. The canonical source is
    # /etc/cosmic/gateway.env (root-owned, 0600), which Alpha cannot read.
    # Mirror it into alpha-agent.env so the operator instructions can include
    # the URL the user actually references — without granting Alpha access
    # to gateway.env's secrets.
    resolved_public_host = first_meaningful_value(
        gateway_public_host,
        external_env.get("GATEWAY_PUBLIC_HOST"),
        existing_env.get("GATEWAY_PUBLIC_HOST"),
        source_data.get("GATEWAY_PUBLIC_HOST"),
        "",
    ) or ""

    overrides = {
        "REDIS_URL": redis_url or "redis://127.0.0.1:6379/0",
        "GATEWAY_URL": gateway_url or "http://127.0.0.1:8080",
        "GATEWAY_INTERNAL_TOKEN": shared_internal_token,
        "GATEWAY_PUBLIC_HOST": resolved_public_host,
        "ORCHESTRATOR_URL": orchestrator_url or "http://127.0.0.1:8743",
        "ORCHESTRATOR_INTERNAL_TOKEN": pick_env("ORCHESTRATOR_INTERNAL_TOKEN", shared_internal_token)
        or shared_internal_token,
        "AGENT_SECRET": signing_secret,
        "INSTANCE_ID": instance_id or ALPHA_AGENT_DEFAULT_INSTANCE_ID,
        "ALPHA_AGENT_ENABLED": pick_env("ALPHA_AGENT_ENABLED", "false") or "false",
        "ALPHA_WORKSPACE_ROOT": pick_env("ALPHA_WORKSPACE_ROOT", "/var/lib/cosmic/alpha")
        or "/var/lib/cosmic/alpha",
        "ALPHA_CODEX_HOME": pick_env("ALPHA_CODEX_HOME", "/var/lib/cosmic/alpha/homes/codex")
        or "/var/lib/cosmic/alpha/homes/codex",
        "ALPHA_CURSOR_HOME": pick_env("ALPHA_CURSOR_HOME", "/var/lib/cosmic/alpha/homes/cursor")
        or "/var/lib/cosmic/alpha/homes/cursor",
        "ALPHA_CODEX_MODEL": pick_env("ALPHA_CODEX_MODEL", "") or "",
        "ALPHA_CURSOR_MODEL": pick_env("ALPHA_CURSOR_MODEL", "composer-2.5") or "composer-2.5",
        "ALPHA_CODEX_SANDBOX": pick_env("ALPHA_CODEX_SANDBOX", "danger-full-access")
        or "danger-full-access",
        "ALPHA_CODEX_TIMEOUT_SEC": pick_env("ALPHA_CODEX_TIMEOUT_SEC", "14400") or "14400",
        "ALPHA_CURSOR_TIMEOUT_SEC": pick_env("ALPHA_CURSOR_TIMEOUT_SEC", "14400") or "14400",
        "ALPHA_CURSOR_INIT_TIMEOUT_SEC": pick_env("ALPHA_CURSOR_INIT_TIMEOUT_SEC", "180") or "180",
        "ALPHA_CLI_IDLE_CHECK_SEC": pick_env("ALPHA_CLI_IDLE_CHECK_SEC", "300") or "300",
        "ALPHA_PROJECT_DB_PATH": pick_env("ALPHA_PROJECT_DB_PATH", "") or "",
        "ALPHA_DOCKER_IMAGE": pick_env("ALPHA_DOCKER_IMAGE", "ubuntu:24.04")
        or "ubuntu:24.04",
        "ALPHA_DOCKER_NETWORK": pick_env("ALPHA_DOCKER_NETWORK", "bridge") or "bridge",
        "ALPHA_DOCKER_MEMORY": pick_env("ALPHA_DOCKER_MEMORY", "4g") or "4g",
        "ALPHA_DOCKER_CPUS": pick_env("ALPHA_DOCKER_CPUS", "2") or "2",
        "ALPHA_DOCKER_PIDS_LIMIT": pick_env("ALPHA_DOCKER_PIDS_LIMIT", "512") or "512",
        "ALPHA_DOCKER_TIMEOUT_SEC": pick_env("ALPHA_DOCKER_TIMEOUT_SEC", "300") or "300",
        "ALPHA_ALLOW_DOCKER_SMOKE": pick_env("ALPHA_ALLOW_DOCKER_SMOKE", "false")
        or "false",
    }

    rendered = render_env_with_overrides(source_raw, overrides)
    rendered_data = parse_env_text(rendered)
    return alpha_agent_system_env_path(system_env_dir), rendered, rendered_data


def alpha_agent_is_configured(env_values: Dict[str, str]) -> bool:
    enabled = meaningful_env_value(env_values.get("ALPHA_AGENT_ENABLED"))
    return (enabled or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def read_alpha_agent_system_env(
    system_env_dir: Optional[Path] = None,
) -> Dict[str, str]:
    env_path = alpha_agent_system_env_path(system_env_dir)
    if not env_path.exists():
        return {}
    return parse_env_text(read_text_file(env_path, use_sudo=True))


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
        raise BootstrapError(
            "Local Redis provisioning currently targets Linux VMs only."
        )
    manager = detect_package_manager()
    if not manager:
        raise BootstrapError(
            "REDIS_URL points to localhost, but no supported package manager was found."
        )
    package_name = PACKAGE_NAMES["redis"].get(manager)
    if not package_name:
        raise BootstrapError(
            "No Redis package mapping for package manager: {0}".format(manager)
        )

    log(
        "Ensuring local Redis is installed for task input queues via {0}: {1}".format(
            manager, package_name
        )
    )
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
        "Installed Redis package, but could not enable a known Redis service for manager {0}.".format(
            manager
        )
    )


def office_renderer_version() -> Optional[str]:
    return executable_version(["soffice", "--version"])


def ensure_office_renderer() -> None:
    version = office_renderer_version()
    if version:
        log("Office renderer available: {0}".format(version))
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError(
            "LibreOffice/soffice missing and no supported Linux package manager was found."
        )

    package_name = "libreoffice"
    log("Installing Office renderer via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    version = office_renderer_version()
    if not version:
        raise BootstrapError(
            "LibreOffice/soffice is still unavailable after installation."
        )
    log("Office renderer available: {0}".format(version))


def pdf_renderer_version() -> Optional[str]:
    return executable_version(["pdftoppm", "-v"])


def ensure_pdf_renderer() -> None:
    version = pdf_renderer_version()
    if version:
        log("PDF renderer available: {0}".format(version))
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError(
            "PDF renderer/pdftoppm missing and no supported Linux package manager was found."
        )

    package_name = PACKAGE_NAMES["poppler"].get(manager)
    if not package_name:
        raise BootstrapError(
            "No poppler package mapping for package manager: {0}".format(manager)
        )

    log("Installing PDF renderer via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    version = pdf_renderer_version()
    if not version:
        raise BootstrapError(
            "PDF renderer/pdftoppm is still unavailable after installation."
        )
    log("PDF renderer available: {0}".format(version))


def ensure_slide_python_build_dependencies() -> None:
    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError(
            "Slide Python build dependencies need a supported Linux package manager."
        )

    packages = SLIDE_PYTHON_BUILD_PACKAGE_NAMES.get(manager)
    if not packages:
        raise BootstrapError(
            "No slide Python build dependency package mapping for package manager: {0}".format(
                manager
            )
        )

    log(
        "Installing slide Python build dependencies via {0}: {1}".format(
            manager, ", ".join(packages)
        )
    )
    install_system_packages(manager, packages)


def missing_required_env_keys(
    env_path: Path, required_keys: Sequence[str]
) -> List[str]:
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


def validate_required_service_env_files(
    effective_sources: Sequence[Tuple[Path, Path]],
) -> None:
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


def normalize_bootstrap_env_payload(
    payload: Dict[str, object],
) -> Dict[str, Dict[str, str]]:
    if not isinstance(payload, dict):
        raise BootstrapError(
            "Supabase bootstrap RPC returned an unexpected payload shape."
        )

    if payload.get("success") is False:
        message = (
            payload.get("message") or payload.get("error") or "Unknown bootstrap error."
        )
        raise BootstrapError("Supabase bootstrap RPC failed: {0}".format(message))

    gateway_env = (
        dict(payload.get("gateway_env") or {})
        if isinstance(payload.get("gateway_env"), dict)
        else {}
    )
    orchestrator_env = (
        dict(payload.get("orchestrator_env") or {})
        if isinstance(payload.get("orchestrator_env"), dict)
        else {}
    )
    model_router_env = (
        dict(payload.get("model_router_env") or {})
        if isinstance(payload.get("model_router_env"), dict)
        else {}
    )
    memory_env = (
        dict(payload.get("memory_env") or {})
        if isinstance(payload.get("memory_env"), dict)
        else {}
    )
    firecrawl_agent_env = {}
    if isinstance(payload.get("firecrawl_agent_env"), dict):
        firecrawl_agent_env = dict(payload.get("firecrawl_agent_env") or {})
    elif isinstance(payload.get("firecrawl_web_scrape_agent_env"), dict):
        firecrawl_agent_env = dict(payload.get("firecrawl_web_scrape_agent_env") or {})
    x_twitter_search_agent_env = {}
    if isinstance(payload.get("x_twitter_search_agent_env"), dict):
        x_twitter_search_agent_env = dict(
            payload.get("x_twitter_search_agent_env") or {}
        )
    tabular_agent_env = {}
    if isinstance(payload.get("tabular_agent_env"), dict):
        tabular_agent_env = dict(payload.get("tabular_agent_env") or {})
    email_agent_env = {}
    if isinstance(payload.get("email_agent_env"), dict):
        email_agent_env = dict(payload.get("email_agent_env") or {})
    gmail_agent_env = {}
    if isinstance(payload.get("gmail_agent_env"), dict):
        gmail_agent_env = dict(payload.get("gmail_agent_env") or {})
    google_docs_agent_env = {}
    if isinstance(payload.get("google_docs_agent_env"), dict):
        google_docs_agent_env = dict(payload.get("google_docs_agent_env") or {})
    google_sheets_agent_env = {}
    if isinstance(payload.get("google_sheets_agent_env"), dict):
        google_sheets_agent_env = dict(payload.get("google_sheets_agent_env") or {})
    image_generator_agent_env = {}
    if isinstance(payload.get("image_generator_agent_env"), dict):
        image_generator_agent_env = dict(payload.get("image_generator_agent_env") or {})
    visual_enhancement_env = (
        dict(payload.get("visual_enhancement_env") or {})
        if isinstance(payload.get("visual_enhancement_env"), dict)
        else {}
    )
    meeting_env = (
        dict(payload.get("meeting_env") or {})
        if isinstance(payload.get("meeting_env"), dict)
        else {}
    )
    vm_payload = (
        dict(payload.get("vm") or {}) if isinstance(payload.get("vm"), dict) else {}
    )

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
        extract_host_from_url(
            vm_payload.get("gateway_url")
            if isinstance(vm_payload.get("gateway_url"), str)
            else None
        ),
    )
    if public_host is not None:
        gateway_env["GATEWAY_PUBLIC_HOST"] = public_host
    owner_user_id = first_meaningful_value(
        gateway_env.get("COSMIC_USER_ID"),
        vm_payload.get("user_id")
        if isinstance(vm_payload.get("user_id"), str)
        else None,
        vm_payload.get("owner_user_id")
        if isinstance(vm_payload.get("owner_user_id"), str)
        else None,
        vm_payload.get("vm_user_id")
        if isinstance(vm_payload.get("vm_user_id"), str)
        else None,
    )
    if owner_user_id is not None:
        gateway_env["COSMIC_USER_ID"] = owner_user_id

    normalized = {
        "gateway.env": gateway_env,
        "model-router.env": model_router_env,
        "orchestrator.env": orchestrator_env,
    }
    if visual_enhancement_env:
        normalized["visual_enhancement.env"] = visual_enhancement_env
    if memory_env:
        normalized["memory.env"] = memory_env
    if firecrawl_agent_env:
        normalized[FIRECRAWL_AGENT_ENV_NAME] = firecrawl_agent_env
    if x_twitter_search_agent_env:
        normalized[X_TWITTER_SEARCH_AGENT_ENV_NAME] = x_twitter_search_agent_env
    if tabular_agent_env:
        normalized[TABULAR_AGENT_ENV_NAME] = tabular_agent_env
    if email_agent_env:
        normalized[EMAIL_AGENT_ENV_NAME] = email_agent_env
    if gmail_agent_env:
        normalized[GMAIL_AGENT_ENV_NAME] = gmail_agent_env
    if google_docs_agent_env:
        normalized[GOOGLE_DOCS_AGENT_ENV_NAME] = google_docs_agent_env
    if google_sheets_agent_env:
        normalized[GOOGLE_SHEETS_AGENT_ENV_NAME] = google_sheets_agent_env
    if image_generator_agent_env:
        normalized[IMAGE_GENERATOR_AGENT_ENV_NAME] = image_generator_agent_env
    required_fields = {
        "gateway.env": (
            "GATEWAY_LOCAL_API_TOKEN",
            "ANTHROPIC_API_KEY",
            "PERPLEXITY_API_KEY",
            "GATEWAY_PUBLIC_HOST",
        ),
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
            "Supabase bootstrap payload is missing required env values: {0}".format(
                ", ".join(missing)
            )
        )

    return normalized


def provision_cosmic_mail_org_via_edge_function(
    *,
    vm_api_token: str,
    supabase_url: str,
    supabase_anon_key: str,
    function_name: str = DEFAULT_COSMIC_MAIL_PROVISION_FUNCTION,
) -> Dict[str, object]:
    """Call the Supabase `provision-cosmic-mail-org` Edge Function.

    The function is the trusted boundary that holds the cosmic-mail admin key. It
    looks up the VM by `vm_api_token` (matched against `public.user_vms.api_token`),
    reads the user's profile, and idempotently creates / adopts a cosmic-mail
    organization, default mailbox on the platform-shared `mail.thelearnchain.com`
    domain, and a default `cosmic` agent. Always mints a fresh org-scoped API key
    and revokes prior ones, so each bootstrap run starts from a clean credential.
    """
    token = meaningful_env_value(vm_api_token)
    base = meaningful_env_value(supabase_url)
    anon_key = meaningful_env_value(supabase_anon_key)
    if token is None:
        raise BootstrapError("vm_api_token is required to provision Cosmic Mail.")
    if base is None or anon_key is None:
        raise BootstrapError(
            "Supabase URL and anon key are required to provision Cosmic Mail."
        )

    function_url = "{0}/functions/v1/{1}".format(base.rstrip("/"), function_name)
    request = Request(
        function_url,
        data=json.dumps({"vm_api_token": token}).encode("utf-8"),
        headers={
            "Authorization": "Bearer {0}".format(anon_key),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    def perform_request() -> str:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")

    try:
        raw = retry_call(
            "Cosmic Mail provisioning Edge Function",
            perform_request,
            retry_exceptions=(HTTPError, URLError),
            should_retry=should_retry_bootstrap_http_error,
        )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BootstrapError(
            "Cosmic Mail provisioning function returned HTTP {0}: {1}".format(
                exc.code, body or exc.reason
            )
        ) from exc
    except URLError as exc:
        raise BootstrapError(
            "Failed to reach Cosmic Mail provisioning function: {0}".format(exc.reason)
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            "Cosmic Mail provisioning function returned invalid JSON."
        ) from exc

    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BootstrapError(
            "Cosmic Mail provisioning function failed: {0}".format(
                json.dumps(payload, default=str)
            )
        )

    return payload


def persist_cosmic_mail_provisioning(payload: Dict[str, object]) -> None:
    """Persist the Edge Function response into the local AgentEmailIntegrationStore.

    Once stored here, all downstream pieces — `gateway/agent_email_integration_store`,
    the gateway adapter reconcile, the email-agent.env override, and the desktop's
    one-click `desktop-config` endpoint — pick up the fresh org key and mailbox
    automatically on the next read.
    """
    base_url = str(payload.get("base_url") or "").strip()
    api_key_obj = payload.get("api_key") if isinstance(payload.get("api_key"), dict) else {}
    plaintext = str((api_key_obj or {}).get("plaintext") or "").strip()  # type: ignore[union-attr]
    mailbox_obj = payload.get("mailbox") if isinstance(payload.get("mailbox"), dict) else {}
    primary_mailbox_address = str((mailbox_obj or {}).get("address") or "").strip()  # type: ignore[union-attr]
    webhook_obj = payload.get("webhook") if isinstance(payload.get("webhook"), dict) else {}
    webhook_secret = (
        str(payload.get("webhook_secret") or "").strip()
        or str((webhook_obj or {}).get("secret") or "").strip()  # type: ignore[union-attr]
        or secrets.token_urlsafe(32)
    )

    if not base_url or not plaintext:
        raise BootstrapError(
            "Cosmic Mail provisioning response is missing base_url or api_key.plaintext."
        )

    store = AgentEmailIntegrationStore(agent_email_integrations_db_path())
    store.save_primary(
        base_url=base_url,
        api_token=plaintext,
        primary_mailbox_address=primary_mailbox_address,
        webhook_secret=webhook_secret,
        webhook_signature_header="X-Cosmic-Mail-Signature",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


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
        raise BootstrapError(
            "Supabase URL and anon key are required to fetch VM env values."
        )

    rpc_url = "{0}/rest/v1/rpc/{1}".format(
        supabase_base.rstrip("/"), DEFAULT_SUPABASE_BOOTSTRAP_RPC
    )
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
            "Supabase bootstrap RPC returned HTTP {0}: {1}".format(
                exc.code, body or exc.reason
            )
        ) from exc
    except URLError as exc:
        raise BootstrapError(
            "Failed to reach Supabase bootstrap RPC: {0}".format(exc.reason)
        ) from exc

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
                current_version[0],
                current_version[1],
                current_version[2],
                sys.executable,
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

        log(
            "Supported Python not found. Installing {0} via {1}.".format(
                package_name, manager
            )
        )
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
            raise BootstrapError(
                "Failed to install pip with ensurepip and no supported package manager was found."
            )

        package_name = PACKAGE_NAMES["pip"].get(manager)
        if not package_name:
            raise BootstrapError(
                "No pip package mapping for package manager: {0}".format(manager)
            )

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
        raise BootstrapError(
            "Python venv module is missing and no supported Linux package manager was found."
        )

    package_name = PACKAGE_NAMES["venv"].get(manager)
    if not package_name:
        raise BootstrapError(
            "No venv package mapping for package manager: {0}".format(manager)
        )

    log("Installing venv support via {0}: {1}".format(manager, package_name))
    install_system_packages(manager, [package_name])

    if not can_create_virtualenv():
        raise BootstrapError(
            "Python venv support is still unavailable after installation attempts."
        )


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
        raise BootstrapError(
            "Missing venv python executable at {0}".format(python_path)
        )

    if not venv_has_pip(venv_path):
        log("pip missing inside virtual environment. Trying ensurepip.")
        run([str(python_path), "-m", "ensurepip", "--upgrade"])
        if not venv_has_pip(venv_path):
            raise BootstrapError(
                "pip is still unavailable inside the virtual environment at {0}".format(
                    venv_path
                )
            )

    run_with_retry(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
        ]
    )


def install_python_requirements(venv_path: Path, requirements_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError(
            "Missing venv python executable at {0}".format(python_path)
        )
    if not requirements_path.exists():
        raise BootstrapError(
            "Missing requirements file at {0}".format(requirements_path)
        )

    log("Installing backend Python dependencies from {0}".format(requirements_path))
    run_with_retry(
        [str(python_path), "-m", "pip", "install", "-r", str(requirements_path)]
    )


def verify_critical_backend_dependencies(venv_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError(
            "Missing venv python executable at {0}".format(python_path)
        )

    for module_name, check_label in CRITICAL_VENV_IMPORT_CHECKS:
        display_command = [str(python_path), "-c", "import {0}".format(module_name)]
        try:
            run(
                [
                    str(python_path),
                    "-c",
                    "import importlib; importlib.import_module({0!r})".format(
                        module_name
                    ),
                ],
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            combined_output = "\n".join(
                part.strip()
                for part in (exc.stdout or "", exc.stderr or "")
                if part and part.strip()
            ).strip()
            details = combined_output or str(exc)
            raise BootstrapError(
                "Critical dependency check failed for {0} ({1}): {2}".format(
                    module_name,
                    check_label,
                    details,
                )
            ) from exc
        log("Verified {0} import for {1}".format(module_name, check_label))


def playwright_chromium_launchable(venv_path: Path) -> bool:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        return False

    check_script = """
import sys
from playwright.sync_api import sync_playwright

errors = []
pw = sync_playwright().start()
try:
    for kwargs in (
        {"headless": True},
        {"headless": True, "channel": "msedge"},
        {"headless": True, "channel": "chrome"},
    ):
        browser = None
        try:
            browser = pw.chromium.launch(**kwargs)
        except Exception as exc:
            errors.append(str(exc))
            continue
        finally:
            if browser is not None:
                browser.close()
        sys.exit(0)
finally:
    pw.stop()

print(" | ".join(errors), file=sys.stderr)
sys.exit(1)
""".strip()
    try:
        run([str(python_path), "-c", check_script], capture_output=True)
        return True
    except (BootstrapError, subprocess.CalledProcessError, FileNotFoundError):
        return False


def ensure_playwright_chromium(venv_path: Path) -> None:
    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError(
            "Missing venv python executable at {0}".format(python_path)
        )

    if playwright_chromium_launchable(venv_path):
        log("Playwright Chromium browser is launchable.")
        return

    log("Installing Playwright Chromium browser dependencies.")
    try:
        run_with_retry(
            [str(python_path), "-m", "playwright", "install-deps", "chromium"],
            use_sudo=True,
        )
    except subprocess.CalledProcessError as exc:
        log(
            "Playwright install-deps failed; continuing with browser install and final launch check: {0}".format(
                exc
            )
        )

    log("Installing Playwright Chromium browser.")
    run_with_retry([str(python_path), "-m", "playwright", "install", "chromium"])

    if not playwright_chromium_launchable(venv_path):
        raise BootstrapError(
            "Playwright Chromium is still unavailable or not launchable after installation."
        )
    log("Playwright Chromium browser is launchable.")


def has_node() -> bool:
    return executable_version(["node", "--version"]) is not None


def has_npm() -> bool:
    return executable_version(["npm", "--version"]) is not None


def ensure_node_toolchain() -> None:
    node_version = executable_version(["node", "--version"])
    npm_version = executable_version(["npm", "--version"])
    node_major = node_major_version(node_version)
    if (
        node_version
        and npm_version
        and node_major is not None
        and node_major >= MIN_NODE_MAJOR
    ):
        log("Node available: {0}".format(node_version))
        log("npm available: {0}".format(npm_version))
        return

    manager = detect_package_manager()
    if not is_linux() or not manager:
        raise BootstrapError(
            "Node.js/npm missing and no supported Linux package manager was found."
        )

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
            run_with_retry(
                [
                    "curl",
                    "-fsSL",
                    "https://deb.nodesource.com/setup_20.x",
                    "-o",
                    str(setup_script),
                ]
            )
            run(["bash", str(setup_script)], use_sudo=True)
            run_with_retry(["apt-get", "install", "-y", "nodejs"], use_sudo=True)
        finally:
            setup_script.unlink(missing_ok=True)

        node_version = executable_version(["node", "--version"])
        npm_version = executable_version(["npm", "--version"])
        node_major = node_major_version(node_version)
        if (
            node_version
            and npm_version
            and node_major is not None
            and node_major >= MIN_NODE_MAJOR
        ):
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
            raise BootstrapError(
                "No Node.js package mapping for package manager: {0}".format(manager)
            )
        packages.append(package_name)
    if not npm_version:
        package_name = PACKAGE_NAMES["npm"].get(manager)
        if not package_name:
            raise BootstrapError(
                "No npm package mapping for package manager: {0}".format(manager)
            )
        packages.append(package_name)

    log(
        "Installing Node.js toolchain via {0}: {1}".format(manager, ", ".join(packages))
    )
    install_system_packages(manager, packages)

    node_version = executable_version(["node", "--version"])
    npm_version = executable_version(["npm", "--version"])
    node_major = node_major_version(node_version)
    if (
        not node_version
        or not npm_version
        or node_major is None
        or node_major < MIN_NODE_MAJOR
    ):
        raise BootstrapError(
            "Node.js/npm are still unavailable or too old after installation attempts. Need Node.js {0}+, got {1}.".format(
                MIN_NODE_MAJOR,
                node_version or "missing",
            )
        )


def ensure_openai_codex_cli() -> None:
    ensure_node_toolchain()
    codex_version = executable_version(["codex", "--version"])
    if codex_version:
        log("OpenAI Codex CLI available: {0}".format(codex_version))
        return

    log("Installing OpenAI Codex CLI globally for Alpha agent execution.")
    run_with_retry(
        ["npm", "install", "-g", OPENAI_CODEX_CLI_PACKAGE],
        use_sudo=True,
    )
    codex_version = executable_version(["codex", "--version"])
    if not codex_version:
        raise BootstrapError("OpenAI Codex CLI is still unavailable after npm install.")
    log("OpenAI Codex CLI available: {0}".format(codex_version))


def ensure_cursor_cli() -> None:
    cursor_version = executable_version(["cursor-agent", "--version"])
    if cursor_version:
        log("Cursor CLI available: {0}".format(cursor_version))
        ensure_cursor_cli_default_config()
        return

    manager = detect_package_manager()
    if manager is not None:
        packages = ["ca-certificates", "curl"]
        if manager == "apk":
            packages.append("bash")
        install_system_packages(manager, packages)

    log("Installing Cursor CLI for Alpha agent execution.")
    run_with_retry(
        ["bash", "-lc", "curl {0} -fsS | bash".format(CURSOR_CLI_INSTALL_URL)],
        capture_output=False,
    )
    cursor_version = executable_version(["cursor-agent", "--version"])
    if not cursor_version:
        local_cursor = Path.home() / ".local" / "bin" / "cursor-agent"
        if local_cursor.exists():
            cursor_version = executable_version([str(local_cursor), "--version"])
    if not cursor_version:
        raise BootstrapError("Cursor CLI is still unavailable after install.")
    log("Cursor CLI available: {0}".format(cursor_version))
    ensure_cursor_cli_default_config()


def ensure_cursor_cli_default_config(cursor_home: Path = DEFAULT_ALPHA_CURSOR_HOME) -> None:
    try:
        config_path, changed, _config = ensure_cursor_cli_non_fast_config(cursor_home)
    except OSError as exc:
        log("WARNING: unable to update Cursor CLI non-Fast config: {0}".format(exc))
        return
    action = "Updated" if changed else "Verified"
    log("{0} Cursor CLI non-Fast config: {1}".format(action, config_path))


def load_package_json(package_json: Path) -> dict:
    try:
        return json.loads(package_json.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError(
            "Missing package.json at {0}".format(package_json)
        ) from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            "Invalid package.json at {0}: {1}".format(package_json, exc)
        ) from exc


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
        (
            visual_enhancement_repo_env_path(),
            system_env_dir / "visual_enhancement.env",
        ),
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
        (
            BACKEND_ROOT / "model_router.env.example",
            system_env_dir / "model-router.env",
        ),
        (
            BACKEND_ROOT / "orchestrator.env.example",
            system_env_dir / "orchestrator.env",
        ),
        (
            BACKEND_ROOT / "visual_enhancement.env.example",
            system_env_dir / "visual_enhancement.env",
        ),
        (DEFAULT_BRIDGE_DIR / ".env.example", system_env_dir / "whatsapp-bridge.env"),
    ]
    if include_memory:
        specs.append(
            (BACKEND_ROOT / "memory.env.example", system_env_dir / "memory.env")
        )
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
    for source, dest in service_env_specs(
        system_env_dir, include_memory=include_memory
    ):
        effective_sources.append(
            (source if source.exists() else fallback_sources[dest], dest)
        )
    return effective_sources


def first_meaningful_value(*values: Optional[str]) -> Optional[str]:
    for value in values:
        normalized = meaningful_env_value(value)
        if normalized is not None:
            return normalized
    return None


def generate_safe_secret(length: int = 40) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_service_env_overrides(
    effective_sources: Sequence[Tuple[Path, Path]],
    *,
    include_memory: bool = False,
    existing_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
    external_env_by_name: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Dict[str, str]]:
    existing_env_by_name = existing_env_by_name or {}
    external_env_by_name = external_env_by_name or {}
    gateway_source = next(
        source for source, dest in effective_sources if dest.name == "gateway.env"
    )
    model_router_source = next(
        source for source, dest in effective_sources if dest.name == "model-router.env"
    )
    orchestrator_source = next(
        source for source, dest in effective_sources if dest.name == "orchestrator.env"
    )
    bridge_source = next(
        source
        for source, dest in effective_sources
        if dest.name == "whatsapp-bridge.env"
    )
    memory_source = None
    if include_memory:
        memory_source = next(
            source for source, dest in effective_sources if dest.name == "memory.env"
        )
    gateway_data = parse_env_text(gateway_source.read_text(encoding="utf-8"))
    model_router_data = parse_env_text(model_router_source.read_text(encoding="utf-8"))
    orchestrator_data = parse_env_text(orchestrator_source.read_text(encoding="utf-8"))
    bridge_data = parse_env_text(bridge_source.read_text(encoding="utf-8"))
    memory_data = (
        parse_env_text(memory_source.read_text(encoding="utf-8"))
        if memory_source is not None
        else {}
    )
    gateway_existing = existing_env_by_name.get("gateway.env", {})
    model_router_existing = existing_env_by_name.get("model-router.env", {})
    orchestrator_existing = existing_env_by_name.get("orchestrator.env", {})
    bridge_existing = existing_env_by_name.get("whatsapp-bridge.env", {})
    memory_existing = existing_env_by_name.get("memory.env", {})
    slide_existing_env = existing_env_by_name.get(SLIDE_AGENT_ENV_NAME, {})
    gmail_existing_env = existing_env_by_name.get(GMAIL_AGENT_ENV_NAME, {})
    gateway_external = external_env_by_name.get("gateway.env", {})
    model_router_external = external_env_by_name.get("model-router.env", {})
    orchestrator_external = external_env_by_name.get("orchestrator.env", {})
    bridge_external = external_env_by_name.get("whatsapp-bridge.env", {})
    memory_external = external_env_by_name.get("memory.env", {})
    slide_external_env = external_env_by_name.get(SLIDE_AGENT_ENV_NAME, {})
    gmail_external_env = external_env_by_name.get(GMAIL_AGENT_ENV_NAME, {})

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
    gmail_webhook_secret = first_meaningful_value(
        gateway_external.get("GATEWAY_GMAIL_WEBHOOK_SECRET"),
        gateway_external.get("GMAIL_WEBHOOK_SECRET"),
        gmail_external_env.get("GMAIL_WEBHOOK_SECRET"),
        gateway_existing.get("GATEWAY_GMAIL_WEBHOOK_SECRET"),
        gateway_existing.get("GMAIL_WEBHOOK_SECRET"),
        gmail_existing_env.get("GMAIL_WEBHOOK_SECRET"),
        gateway_data.get("GATEWAY_GMAIL_WEBHOOK_SECRET"),
        gateway_data.get("GMAIL_WEBHOOK_SECRET"),
        secrets.token_urlsafe(32),
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
    orchestrator_fireworks_api_key = first_meaningful_value(
        orchestrator_external.get("ORCHESTRATOR_FIREWORKS_API_KEY"),
        orchestrator_external.get("FIREWORKS_API_KEY"),
        orchestrator_external.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        orchestrator_existing.get("ORCHESTRATOR_FIREWORKS_API_KEY"),
        orchestrator_existing.get("FIREWORKS_API_KEY"),
        orchestrator_existing.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        orchestrator_data.get("ORCHESTRATOR_FIREWORKS_API_KEY"),
        orchestrator_data.get("FIREWORKS_API_KEY"),
        orchestrator_data.get("VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"),
        gateway_external.get("FIREWORKS_API_KEY"),
        gateway_existing.get("FIREWORKS_API_KEY"),
        gateway_data.get("FIREWORKS_API_KEY"),
        slide_external_env.get("FIREWORKS_API_KEY"),
        slide_existing_env.get("FIREWORKS_API_KEY"),
    )
    orchestrator_fireworks_base_url = first_meaningful_value(
        orchestrator_external.get("ORCHESTRATOR_FIREWORKS_BASE_URL"),
        orchestrator_external.get("FIREWORKS_BASE_URL"),
        orchestrator_existing.get("ORCHESTRATOR_FIREWORKS_BASE_URL"),
        orchestrator_existing.get("FIREWORKS_BASE_URL"),
        orchestrator_data.get("ORCHESTRATOR_FIREWORKS_BASE_URL"),
        orchestrator_data.get("FIREWORKS_BASE_URL"),
        slide_external_env.get("FIREWORKS_BASE_URL"),
        slide_existing_env.get("FIREWORKS_BASE_URL"),
        "https://api.fireworks.ai/inference/v1",
    )
    orchestrator_fireworks_kimi_model = first_meaningful_value(
        orchestrator_external.get("ORCHESTRATOR_FIREWORKS_KIMI_MODEL"),
        orchestrator_external.get("FIREWORKS_KIMI_MODEL"),
        orchestrator_existing.get("ORCHESTRATOR_FIREWORKS_KIMI_MODEL"),
        orchestrator_existing.get("FIREWORKS_KIMI_MODEL"),
        orchestrator_data.get("ORCHESTRATOR_FIREWORKS_KIMI_MODEL"),
        orchestrator_data.get("FIREWORKS_KIMI_MODEL"),
        slide_external_env.get("FIREWORKS_KIMI_MODEL"),
        slide_existing_env.get("FIREWORKS_KIMI_MODEL"),
        "accounts/fireworks/models/kimi-k2p6",
    )
    cosmic_orchestrator_default_provider = first_meaningful_value(
        orchestrator_external.get("COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER"),
        orchestrator_existing.get("COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER"),
        orchestrator_data.get("COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER"),
        "anthropic",
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
    gateway_xai_api_key = first_meaningful_value(
        gateway_external.get("XAI_API_KEY"),
        gateway_existing.get("XAI_API_KEY"),
        gateway_data.get("XAI_API_KEY"),
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
    )
    memory_graph_warm_cache_on_startup = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_WARM_CACHE_ON_STARTUP"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_WARM_CACHE_ON_STARTUP"),
        memory_data.get("COSMIC_MEMORY_GRAPH_WARM_CACHE_ON_STARTUP"),
    )
    memory_graph_deterministic_enabled = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        memory_existing.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        memory_data.get("COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED"),
        "true",
    )
    memory_neo4j_uri = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_NEO4J_URI"),
        memory_existing.get("COSMIC_MEMORY_NEO4J_URI"),
        memory_data.get("COSMIC_MEMORY_NEO4J_URI"),
    )
    memory_neo4j_username = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_NEO4J_USERNAME"),
        memory_existing.get("COSMIC_MEMORY_NEO4J_USERNAME"),
        memory_data.get("COSMIC_MEMORY_NEO4J_USERNAME"),
    )
    memory_neo4j_password = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_NEO4J_PASSWORD"),
        memory_existing.get("COSMIC_MEMORY_NEO4J_PASSWORD"),
        memory_data.get("COSMIC_MEMORY_NEO4J_PASSWORD"),
    )
    memory_neo4j_database = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_NEO4J_DATABASE"),
        memory_existing.get("COSMIC_MEMORY_NEO4J_DATABASE"),
        memory_data.get("COSMIC_MEMORY_NEO4J_DATABASE"),
        "neo4j",
    )
    memory_primary_user_display_name = first_meaningful_value(
        memory_external.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        memory_existing.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        memory_data.get("COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME"),
        "",
    )
    if (memory_graph_backend or "").strip().lower() in {"", "memory"} and any(
        (memory_neo4j_uri, memory_neo4j_username, memory_neo4j_password)
    ):
        memory_graph_backend = "neo4j"
    if (memory_graph_backend or "").strip().lower() == "neo4j":
        memory_neo4j_uri = memory_neo4j_uri or DEFAULT_NEO4J_URI
        memory_neo4j_username = memory_neo4j_username or DEFAULT_NEO4J_USERNAME
        memory_neo4j_database = memory_neo4j_database or DEFAULT_NEO4J_DATABASE
        memory_neo4j_password = memory_neo4j_password or generate_safe_secret()
    if not memory_graph_warm_cache_on_startup:
        memory_graph_warm_cache_on_startup = (
            "true"
            if (memory_graph_backend or "").strip().lower() == "neo4j"
            else "false"
        )
    if not memory_graph_sync_on_startup:
        memory_graph_sync_on_startup = (
            "false"
            if (memory_graph_backend or "").strip().lower() == "neo4j"
            else "true"
        )

    overrides = {
        "gateway.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token
            or secrets.token_urlsafe(32),
            "GATEWAY_LOCAL_API_TOKEN": local_api_token or secrets.token_urlsafe(24),
            "GATEWAY_SIGNING_SECRET": signing_secret or secrets.token_urlsafe(32),
            "WHATSAPP_BRIDGE_TOKEN": bridge_token or secrets.token_urlsafe(32),
            "ANTHROPIC_API_KEY": shared_anthropic_api_key or "<anthropic-api-key>",
            "PERPLEXITY_API_KEY": perplexity_api_key or "<perplexity-api-key>",
            "XAI_API_KEY": gateway_xai_api_key or "",
            "GATEWAY_PUBLIC_HOST": gateway_public_host or "<gateway.user.example.com>",
            "GATEWAY_GMAIL_WEBHOOK_SECRET": gmail_webhook_secret or "",
            "HAIKU_MODEL": haiku_model or "claude-haiku-4-5",
            "ENABLE_PUSH_NOTIFICATIONS": gateway_external.get("ENABLE_PUSH_NOTIFICATIONS")
            or gateway_existing.get("ENABLE_PUSH_NOTIFICATIONS")
            or gateway_data.get("ENABLE_PUSH_NOTIFICATIONS")
            or "true",
            "EXPO_ACCESS_TOKEN": gateway_external.get("EXPO_ACCESS_TOKEN")
            or gateway_existing.get("EXPO_ACCESS_TOKEN")
            or gateway_data.get("EXPO_ACCESS_TOKEN")
            or "",
            "EXPO_PUSH_URL": gateway_external.get("EXPO_PUSH_URL")
            or gateway_existing.get("EXPO_PUSH_URL")
            or gateway_data.get("EXPO_PUSH_URL")
            or "https://exp.host/--/api/v2/push/send",
            "EXPO_PUSH_TIMEOUT_SEC": gateway_external.get("EXPO_PUSH_TIMEOUT_SEC")
            or gateway_existing.get("EXPO_PUSH_TIMEOUT_SEC")
            or gateway_data.get("EXPO_PUSH_TIMEOUT_SEC")
            or "8",
            "FCM_PROJECT_ID": gateway_external.get("FCM_PROJECT_ID")
            or gateway_existing.get("FCM_PROJECT_ID")
            or gateway_data.get("FCM_PROJECT_ID")
            or "",
            "FCM_SERVICE_ACCOUNT_FILE": gateway_external.get("FCM_SERVICE_ACCOUNT_FILE")
            or gateway_existing.get("FCM_SERVICE_ACCOUNT_FILE")
            or gateway_data.get("FCM_SERVICE_ACCOUNT_FILE")
            or "",
            "FCM_SERVICE_ACCOUNT_JSON": gateway_external.get("FCM_SERVICE_ACCOUNT_JSON")
            or gateway_existing.get("FCM_SERVICE_ACCOUNT_JSON")
            or gateway_data.get("FCM_SERVICE_ACCOUNT_JSON")
            or "",
            "FCM_TIMEOUT_SEC": gateway_external.get("FCM_TIMEOUT_SEC")
            or gateway_existing.get("FCM_TIMEOUT_SEC")
            or gateway_data.get("FCM_TIMEOUT_SEC")
            or "8",
            "MOBILE_PRESENCE_STALE_SEC": gateway_external.get("MOBILE_PRESENCE_STALE_SEC")
            or gateway_existing.get("MOBILE_PRESENCE_STALE_SEC")
            or gateway_data.get("MOBILE_PRESENCE_STALE_SEC")
            or "120",
        },
        "model-router.env": {
            "GROQ_API_KEY": groq_api_key or "<groq-api-key>",
        },
        "orchestrator.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token
            or secrets.token_urlsafe(32),
            "GATEWAY_SIGNING_SECRET": signing_secret or secrets.token_urlsafe(32),
            "ANTHROPIC_API_KEY": shared_anthropic_api_key or "<anthropic-api-key>",
            "ANTHROPIC_MODEL": opus_model or "claude-opus-4-6",
            "COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER": cosmic_orchestrator_default_provider
            or "anthropic",
            "ORCHESTRATOR_FIREWORKS_API_KEY": orchestrator_fireworks_api_key or "",
            "ORCHESTRATOR_FIREWORKS_BASE_URL": orchestrator_fireworks_base_url
            or "https://api.fireworks.ai/inference/v1",
            "ORCHESTRATOR_FIREWORKS_KIMI_MODEL": orchestrator_fireworks_kimi_model
            or "accounts/fireworks/models/kimi-k2p6",
            "ORCHESTRATOR_CODE_SANDBOX_ENABLED": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_ENABLED")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_ENABLED")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_ENABLED")
            or "true",
            "ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_TIMEOUT_SEC")
            or "45",
            "ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_NETWORK")
            or "false",
            "ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_ALLOW_PIP")
            or "true",
            "ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_PIP_TIMEOUT_SEC")
            or "120",
            "ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_VENV_CACHE_ROOT")
            or "",
            "ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_MAX_SCRIPT_BYTES")
            or "256000",
            "ORCHESTRATOR_CODE_SANDBOX_MAX_FILES": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILES")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILES")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILES")
            or "12",
            "ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES": orchestrator_external.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES")
            or orchestrator_existing.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES")
            or orchestrator_data.get("ORCHESTRATOR_CODE_SANDBOX_MAX_FILE_BYTES")
            or "26214400",
        },
        "whatsapp-bridge.env": {
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token
            or secrets.token_urlsafe(32),
            "WHATSAPP_BRIDGE_TOKEN": bridge_token or secrets.token_urlsafe(32),
            "WHATSAPP_AUTH_DIR": whatsapp_auth_dir or str(DEFAULT_WHATSAPP_AUTH_DIR),
        },
    }
    if include_memory:
        overrides["gateway.env"]["COSMIC_MEMORY_URL"] = (
            memory_url or "http://127.0.0.1:8090"
        )
        overrides["memory.env"] = {
            "PERPLEXITY_API_KEY": memory_perplexity_api_key or "<perplexity-api-key>",
            "XAI_API_KEY": memory_xai_api_key or "",
            "GATEWAY_INTERNAL_TOKEN": shared_internal_token
            or secrets.token_urlsafe(32),
            "COSMIC_MEMORY_INTERNAL_TOKEN": shared_internal_token
            or secrets.token_urlsafe(32),
            "COSMIC_MEMORY_DATA_DIR": memory_data_dir or str(DEFAULT_MEMORY_DATA_DIR),
            "COSMIC_MEMORY_SYNC_ON_STARTUP": memory_sync_on_startup or "true",
            "COSMIC_MEMORY_GRAPH_SYNC_ON_STARTUP": memory_graph_sync_on_startup
            or "true",
            "COSMIC_MEMORY_GRAPH_WARM_CACHE_ON_STARTUP": memory_graph_warm_cache_on_startup
            or "false",
            "COSMIC_MEMORY_GRAPH_DETERMINISTIC_ENABLED": memory_graph_deterministic_enabled
            or "true",
            "COSMIC_MEMORY_GRAPH_EXTRACT_ENABLED": memory_graph_extract_enabled
            or "false",
            "COSMIC_MEMORY_GRAPH_BACKEND": memory_graph_backend or "memory",
            "COSMIC_MEMORY_NEO4J_URI": memory_neo4j_uri or "",
            "COSMIC_MEMORY_NEO4J_USERNAME": memory_neo4j_username or "",
            "COSMIC_MEMORY_NEO4J_PASSWORD": memory_neo4j_password or "",
            "COSMIC_MEMORY_NEO4J_DATABASE": memory_neo4j_database or "neo4j",
            "COSMIC_MEMORY_PRIMARY_USER_DISPLAY_NAME": memory_primary_user_display_name
            or "",
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

    # Provision Cosmic Mail before computing service env overrides — the email-agent.env
    # build pulls COSMIC_MAIL_* values from `AgentEmailIntegrationStore`, so persisting
    # the provisioning result here makes the rest of bootstrap pick it up automatically.
    # Best-effort: a Cosmic Mail outage must not block the rest of bootstrap.
    gateway_env_overrides = external_env_by_name.get("gateway.env", {}) or {}
    vm_api_token = meaningful_env_value(gateway_env_overrides.get("GATEWAY_LOCAL_API_TOKEN"))
    if vm_api_token is not None:
        try:
            cosmic_mail_payload = provision_cosmic_mail_org_via_edge_function(
                vm_api_token=vm_api_token,
                supabase_url=supabase_url,
                supabase_anon_key=supabase_anon_key,
            )
            persist_cosmic_mail_provisioning(cosmic_mail_payload)
            log(
                "Provisioned Cosmic Mail org for VM: org={0} mailbox={1}".format(
                    (cosmic_mail_payload.get("organization") or {}).get("id"),
                    (cosmic_mail_payload.get("mailbox") or {}).get("address"),
                )
            )
        except BootstrapError as exc:
            log(
                "WARNING: Cosmic Mail provisioning skipped — local stack will boot "
                "without an Agent Email integration. Re-run bootstrap to retry. "
                "Error: {0}".format(exc)
            )
    else:
        log(
            "WARNING: Cosmic Mail provisioning skipped — bootstrap response is "
            "missing GATEWAY_LOCAL_API_TOKEN."
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
            existing_env_by_name[dest_path.name] = parse_env_text(
                repo_path.read_text(encoding="utf-8")
            )
        elif source_path.exists():
            existing_env_by_name[dest_path.name] = parse_env_text(
                source_path.read_text(encoding="utf-8")
            )

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
        overrides = overrides_by_dest.get(
            dest_path.name,
            external_env_by_name.get(dest_path.name, {}),
        )
        rendered = render_env_with_overrides(
            raw_source_path.read_text(encoding="utf-8"),
            overrides,
        )
        repo_path.write_text(rendered, encoding="utf-8")
        written.append(repo_path)
        log(
            "Materialized repo env file from Supabase bootstrap payload: {0}".format(
                repo_path
            )
        )

    firecrawl_repo_path = firecrawl_agent_repo_env_path()
    _firecrawl_dest_path, firecrawl_rendered, _firecrawl_env = (
        build_firecrawl_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    firecrawl_repo_path.parent.mkdir(parents=True, exist_ok=True)
    firecrawl_repo_path.write_text(firecrawl_rendered, encoding="utf-8")
    written.append(firecrawl_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            firecrawl_repo_path
        )
    )

    docs_parser_repo_path = docs_parser_agent_repo_env_path()
    _docs_parser_dest_path, docs_parser_rendered, _docs_parser_env = (
        build_docs_parser_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    docs_parser_repo_path.parent.mkdir(parents=True, exist_ok=True)
    docs_parser_repo_path.write_text(docs_parser_rendered, encoding="utf-8")
    written.append(docs_parser_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            docs_parser_repo_path
        )
    )

    x_twitter_repo_path = x_twitter_search_agent_repo_env_path()
    _x_twitter_dest_path, x_twitter_rendered, _x_twitter_env = (
        build_x_twitter_search_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    x_twitter_repo_path.parent.mkdir(parents=True, exist_ok=True)
    x_twitter_repo_path.write_text(x_twitter_rendered, encoding="utf-8")
    written.append(x_twitter_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            x_twitter_repo_path
        )
    )

    tabular_repo_path = tabular_agent_repo_env_path()
    _tabular_dest_path, tabular_rendered, _tabular_env = (
        build_tabular_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    tabular_repo_path.parent.mkdir(parents=True, exist_ok=True)
    tabular_repo_path.write_text(tabular_rendered, encoding="utf-8")
    written.append(tabular_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            tabular_repo_path
        )
    )

    email_repo_path = email_agent_repo_env_path()
    _email_dest_path, email_rendered, _email_env = build_email_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=external_env_by_name,
    )
    email_repo_path.parent.mkdir(parents=True, exist_ok=True)
    email_repo_path.write_text(email_rendered, encoding="utf-8")
    written.append(email_repo_path)
    log("Materialized repo env file from bootstrap inputs: {0}".format(email_repo_path))

    image_generator_repo_path = image_generator_agent_repo_env_path()
    _image_dest_path, image_rendered, _image_env = (
        build_image_generator_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    image_generator_repo_path.parent.mkdir(parents=True, exist_ok=True)
    image_generator_repo_path.write_text(image_rendered, encoding="utf-8")
    written.append(image_generator_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            image_generator_repo_path
        )
    )

    visual_repo_path = visual_enhancement_repo_env_path()
    _visual_dest_path, visual_rendered, _visual_env = (
        build_visual_enhancement_env_rendered(
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    visual_repo_path.parent.mkdir(parents=True, exist_ok=True)
    visual_repo_path.write_text(visual_rendered, encoding="utf-8")
    written.append(visual_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            visual_repo_path
        )
    )

    calendar_repo_path = calendar_agent_repo_env_path()
    _calendar_dest_path, calendar_rendered, _calendar_env = (
        build_calendar_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    calendar_repo_path.parent.mkdir(parents=True, exist_ok=True)
    calendar_repo_path.write_text(calendar_rendered, encoding="utf-8")
    written.append(calendar_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            calendar_repo_path
        )
    )

    gmail_repo_path = gmail_agent_repo_env_path()
    gmail_external_for_render = dict(external_env_by_name.get(GMAIL_AGENT_ENV_NAME, {}))
    if overrides_by_dest.get("gateway.env", {}).get("GATEWAY_GMAIL_WEBHOOK_SECRET"):
        gmail_external_for_render.setdefault(
            "GMAIL_WEBHOOK_SECRET",
            overrides_by_dest["gateway.env"]["GATEWAY_GMAIL_WEBHOOK_SECRET"],
        )
    gmail_external_envs = {
        **external_env_by_name,
        GMAIL_AGENT_ENV_NAME: gmail_external_for_render,
    }
    _gmail_dest_path, gmail_rendered, _gmail_env = build_gmail_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=gmail_external_envs,
    )
    gmail_repo_path.parent.mkdir(parents=True, exist_ok=True)
    gmail_repo_path.write_text(gmail_rendered, encoding="utf-8")
    written.append(gmail_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            gmail_repo_path
        )
    )

    google_docs_repo_path = google_docs_agent_repo_env_path()
    _google_docs_dest_path, google_docs_rendered, _google_docs_env = (
        build_google_docs_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    google_docs_repo_path.parent.mkdir(parents=True, exist_ok=True)
    google_docs_repo_path.write_text(google_docs_rendered, encoding="utf-8")
    written.append(google_docs_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            google_docs_repo_path
        )
    )

    google_sheets_repo_path = google_sheets_agent_repo_env_path()
    _google_sheets_dest_path, google_sheets_rendered, _google_sheets_env = (
        build_google_sheets_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    google_sheets_repo_path.parent.mkdir(parents=True, exist_ok=True)
    google_sheets_repo_path.write_text(google_sheets_rendered, encoding="utf-8")
    written.append(google_sheets_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            google_sheets_repo_path
        )
    )

    diagram_repo_path = diagram_agent_repo_env_path()
    _diagram_dest_path, diagram_rendered, _diagram_env = (
        build_diagram_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=existing_env_by_name,
            external_env_by_name=external_env_by_name,
        )
    )
    diagram_repo_path.parent.mkdir(parents=True, exist_ok=True)
    diagram_repo_path.write_text(diagram_rendered, encoding="utf-8")
    written.append(diagram_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            diagram_repo_path
        )
    )

    map_repo_path = map_agent_repo_env_path()
    _map_dest_path, map_rendered, _map_env = build_map_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"]["GATEWAY_INTERNAL_TOKEN"],
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=external_env_by_name,
    )
    map_repo_path.parent.mkdir(parents=True, exist_ok=True)
    map_repo_path.write_text(map_rendered, encoding="utf-8")
    written.append(map_repo_path)
    log("Materialized repo env file from bootstrap inputs: {0}".format(map_repo_path))

    slide_repo_path = slide_agent_repo_env_path()
    _slide_dest_path, slide_rendered, _slide_env = build_slide_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=external_env_by_name,
    )
    slide_repo_path.parent.mkdir(parents=True, exist_ok=True)
    slide_repo_path.write_text(slide_rendered, encoding="utf-8")
    written.append(slide_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            slide_repo_path
        )
    )
    alpha_repo_path = alpha_agent_repo_env_path()
    _alpha_dest_path, alpha_rendered, _alpha_env = build_alpha_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
        external_env_by_name=external_env_by_name,
        gateway_public_host=overrides_by_dest["gateway.env"].get("GATEWAY_PUBLIC_HOST"),
    )
    alpha_repo_path.parent.mkdir(parents=True, exist_ok=True)
    alpha_repo_path.write_text(alpha_rendered, encoding="utf-8")
    written.append(alpha_repo_path)
    log(
        "Materialized repo env file from bootstrap inputs: {0}".format(
            alpha_repo_path
        )
    )
    return written


def install_service_env_files(
    system_env_dir: Path, *, include_memory: bool = False
) -> List[Path]:
    if not is_linux():
        raise BootstrapError(
            "System env provisioning currently targets Linux VMs only."
        )

    external_env_by_name: Dict[str, Dict[str, str]] = {}
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
        rendered = render_env_with_overrides(
            raw, overrides_by_dest.get(dest_path.name, {})
        )
        install_text_file(dest_path, rendered, mode="600", use_sudo=True)

        installed.append(dest_path)
        log("Installed system env file: {0}".format(dest_path))

    firecrawl_dest_path, firecrawl_rendered, _firecrawl_env = (
        build_firecrawl_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(firecrawl_dest_path.parent)], use_sudo=True)
    if firecrawl_dest_path.exists():
        log("System env file already exists: {0}".format(firecrawl_dest_path))
    else:
        install_text_file(
            firecrawl_dest_path, firecrawl_rendered, mode="600", use_sudo=True
        )
        installed.append(firecrawl_dest_path)
        log("Installed system env file: {0}".format(firecrawl_dest_path))

    docs_parser_dest_path, docs_parser_rendered, _docs_parser_env = (
        build_docs_parser_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(
        ["install", "-d", "-m", "755", str(docs_parser_dest_path.parent)], use_sudo=True
    )
    if docs_parser_dest_path.exists():
        log("System env file already exists: {0}".format(docs_parser_dest_path))
    else:
        install_text_file(
            docs_parser_dest_path, docs_parser_rendered, mode="600", use_sudo=True
        )
        installed.append(docs_parser_dest_path)
        log("Installed system env file: {0}".format(docs_parser_dest_path))

    x_twitter_dest_path, x_twitter_rendered, _x_twitter_env = (
        build_x_twitter_search_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(x_twitter_dest_path.parent)], use_sudo=True)
    if x_twitter_dest_path.exists():
        existing_raw = read_text_file(x_twitter_dest_path, use_sudo=True)
        existing_data = parse_env_text(existing_raw)
        rendered_data = parse_env_text(x_twitter_rendered)
        desired_max_posts_raw = rendered_data.get("X_SEARCH_MAX_POSTS") or "30"
        try:
            desired_max_posts = int(desired_max_posts_raw)
        except ValueError:
            desired_max_posts = 30
        try:
            existing_max_posts = int(existing_data.get("X_SEARCH_MAX_POSTS") or "0")
        except ValueError:
            existing_max_posts = 0
        if existing_max_posts < desired_max_posts:
            updated_raw = render_env_with_overrides(
                existing_raw,
                {"X_SEARCH_MAX_POSTS": str(desired_max_posts)},
            )
            install_text_file(
                x_twitter_dest_path, updated_raw, mode="600", use_sudo=True
            )
            installed.append(x_twitter_dest_path)
            log(
                "Updated system env file {0}: X_SEARCH_MAX_POSTS={1}".format(
                    x_twitter_dest_path,
                    desired_max_posts,
                )
            )
        else:
            log("System env file already exists: {0}".format(x_twitter_dest_path))
    else:
        install_text_file(
            x_twitter_dest_path, x_twitter_rendered, mode="600", use_sudo=True
        )
        installed.append(x_twitter_dest_path)
        log("Installed system env file: {0}".format(x_twitter_dest_path))

    tabular_dest_path, tabular_rendered, _tabular_env = (
        build_tabular_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(tabular_dest_path.parent)], use_sudo=True)
    if tabular_dest_path.exists():
        log("System env file already exists: {0}".format(tabular_dest_path))
    else:
        install_text_file(
            tabular_dest_path, tabular_rendered, mode="600", use_sudo=True
        )
        installed.append(tabular_dest_path)
        log("Installed system env file: {0}".format(tabular_dest_path))

    email_dest_path, email_rendered, _email_env = build_email_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
    )
    run(["install", "-d", "-m", "755", str(email_dest_path.parent)], use_sudo=True)
    if email_dest_path.exists():
        log("System env file already exists: {0}".format(email_dest_path))
    else:
        install_text_file(email_dest_path, email_rendered, mode="600", use_sudo=True)
        installed.append(email_dest_path)
        log("Installed system env file: {0}".format(email_dest_path))

    image_dest_path, image_rendered, _image_env = (
        build_image_generator_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(image_dest_path.parent)], use_sudo=True)
    if image_dest_path.exists():
        log("System env file already exists: {0}".format(image_dest_path))
    else:
        install_text_file(image_dest_path, image_rendered, mode="600", use_sudo=True)
        installed.append(image_dest_path)
        log("Installed system env file: {0}".format(image_dest_path))

    calendar_dest_path, calendar_rendered, _calendar_env = (
        build_calendar_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(calendar_dest_path.parent)], use_sudo=True)
    if calendar_dest_path.exists():
        log("System env file already exists: {0}".format(calendar_dest_path))
    else:
        install_text_file(
            calendar_dest_path, calendar_rendered, mode="600", use_sudo=True
        )
        installed.append(calendar_dest_path)
        log("Installed system env file: {0}".format(calendar_dest_path))

    gmail_external_for_install = dict(external_env_by_name.get(GMAIL_AGENT_ENV_NAME, {}))
    if overrides_by_dest.get("gateway.env", {}).get("GATEWAY_GMAIL_WEBHOOK_SECRET"):
        gmail_external_for_install.setdefault(
            "GMAIL_WEBHOOK_SECRET",
            overrides_by_dest["gateway.env"]["GATEWAY_GMAIL_WEBHOOK_SECRET"],
        )
    gmail_install_external_envs = {
        **external_env_by_name,
        GMAIL_AGENT_ENV_NAME: gmail_external_for_install,
    }
    gmail_dest_path, gmail_rendered, _gmail_env = build_gmail_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        external_env_by_name=gmail_install_external_envs,
    )
    run(["install", "-d", "-m", "755", str(gmail_dest_path.parent)], use_sudo=True)
    if gmail_dest_path.exists():
        log("System env file already exists: {0}".format(gmail_dest_path))
    else:
        install_text_file(gmail_dest_path, gmail_rendered, mode="600", use_sudo=True)
        installed.append(gmail_dest_path)
        log("Installed system env file: {0}".format(gmail_dest_path))

    google_docs_dest_path, google_docs_rendered, _google_docs_env = (
        build_google_docs_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(
        ["install", "-d", "-m", "755", str(google_docs_dest_path.parent)],
        use_sudo=True,
    )
    if google_docs_dest_path.exists():
        log("System env file already exists: {0}".format(google_docs_dest_path))
    else:
        install_text_file(
            google_docs_dest_path, google_docs_rendered, mode="600", use_sudo=True
        )
        installed.append(google_docs_dest_path)
        log("Installed system env file: {0}".format(google_docs_dest_path))

    google_sheets_dest_path, google_sheets_rendered, _google_sheets_env = (
        build_google_sheets_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(
        ["install", "-d", "-m", "755", str(google_sheets_dest_path.parent)],
        use_sudo=True,
    )
    if google_sheets_dest_path.exists():
        log("System env file already exists: {0}".format(google_sheets_dest_path))
    else:
        install_text_file(
            google_sheets_dest_path, google_sheets_rendered, mode="600", use_sudo=True
        )
        installed.append(google_sheets_dest_path)
        log("Installed system env file: {0}".format(google_sheets_dest_path))

    diagram_dest_path, diagram_rendered, _diagram_env = (
        build_diagram_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
        )
    )
    run(["install", "-d", "-m", "755", str(diagram_dest_path.parent)], use_sudo=True)
    if diagram_dest_path.exists():
        log("System env file already exists: {0}".format(diagram_dest_path))
    else:
        install_text_file(
            diagram_dest_path, diagram_rendered, mode="600", use_sudo=True
        )
        installed.append(diagram_dest_path)
        log("Installed system env file: {0}".format(diagram_dest_path))

    map_dest_path, map_rendered, _map_env = build_map_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"]["GATEWAY_INTERNAL_TOKEN"],
        system_env_dir=system_env_dir,
    )
    run(["install", "-d", "-m", "755", str(map_dest_path.parent)], use_sudo=True)
    if map_dest_path.exists():
        log("System env file already exists: {0}".format(map_dest_path))
    else:
        install_text_file(map_dest_path, map_rendered, mode="600", use_sudo=True)
        installed.append(map_dest_path)
        log("Installed system env file: {0}".format(map_dest_path))

    slide_dest_path, slide_rendered, _slide_env = build_slide_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
    )
    run(["install", "-d", "-m", "755", str(slide_dest_path.parent)], use_sudo=True)
    if slide_dest_path.exists():
        log("System env file already exists: {0}".format(slide_dest_path))
    else:
        install_text_file(slide_dest_path, slide_rendered, mode="600", use_sudo=True)
        installed.append(slide_dest_path)
        log("Installed system env file: {0}".format(slide_dest_path))

    alpha_dest_path, alpha_rendered, _alpha_env = build_alpha_agent_env_rendered(
        signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
        shared_internal_token=overrides_by_dest["gateway.env"][
            "GATEWAY_INTERNAL_TOKEN"
        ],
        system_env_dir=system_env_dir,
        gateway_public_host=overrides_by_dest["gateway.env"].get("GATEWAY_PUBLIC_HOST"),
    )
    run(["install", "-d", "-m", "755", str(alpha_dest_path.parent)], use_sudo=True)
    if alpha_dest_path.exists():
        log("System env file already exists: {0}".format(alpha_dest_path))
    else:
        install_text_file(alpha_dest_path, alpha_rendered, mode="600", use_sudo=True)
        installed.append(alpha_dest_path)
        log("Installed system env file: {0}".format(alpha_dest_path))

    return installed


def install_whatsapp_bridge_dependencies(bridge_dir: Path) -> None:
    package_json = bridge_dir / "package.json"
    if not bridge_dir.exists():
        raise BootstrapError(
            "WhatsApp bridge directory does not exist: {0}".format(bridge_dir)
        )
    if not package_json.exists():
        raise BootstrapError(
            "Missing WhatsApp bridge package.json at {0}".format(package_json)
        )

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

    scripts = (
        package_data.get("scripts")
        if isinstance(package_data.get("scripts"), dict)
        else {}
    )
    if "build" in scripts:
        log("Running WhatsApp bridge build script in {0}".format(bridge_dir))
        run(["npm", "run", "build"], check=True, capture_output=False, cwd=bridge_dir)


def current_service_user() -> str:
    return os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()


def current_service_home() -> Path:
    service_user = current_service_user()
    if pwd is not None:
        try:
            return Path(pwd.getpwnam(service_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def _extract_missing_chrome_revision(stderr: str) -> Optional[str]:
    match = re.search(r"Could not find Chrome \(ver\. ([^)]+)\)", str(stderr or ""))
    if not match:
        return None
    return match.group(1).strip() or None


def _run_mermaid_smoke(
    *, service_user: str, service_home: Path, cache_dir: Path
) -> None:
    if shutil.which("runuser") is None:
        raise BootstrapError(
            "runuser is required to validate Mermaid rendering as the service user."
        )

    smoke_script = (
        'tmpdir="$(mktemp -d)"; '
        "trap 'rm -rf \"$tmpdir\"' EXIT; "
        "cat >\"$tmpdir/input.mmd\" <<'EOF_MMD'\n"
        "graph TD\n"
        "A-->B\n"
        "EOF_MMD\n"
        "cat >\"$tmpdir/puppeteer-config.json\" <<'EOF_PUPPETEER'\n"
        '{"args":["--no-sandbox","--disable-setuid-sandbox"]}\n'
        "EOF_PUPPETEER\n"
        'mmdc -i "$tmpdir/input.mmd" -o "$tmpdir/output.svg" -p "$tmpdir/puppeteer-config.json"; '
        'test -s "$tmpdir/output.svg"'
    )
    run(
        [
            "runuser",
            "-u",
            service_user,
            "--",
            "env",
            "HOME={0}".format(service_home),
            "PUPPETEER_CACHE_DIR={0}".format(cache_dir),
            "bash",
            "-lc",
            smoke_script,
        ],
        use_sudo=True,
        capture_output=True,
    )


def _install_mermaid_browser_revision(
    *, service_user: str, service_home: Path, cache_dir: Path, revision: str
) -> None:
    if shutil.which("runuser") is None:
        raise BootstrapError(
            "runuser is required to install the Mermaid browser runtime as the service user."
        )
    run_with_retry(
        [
            "runuser",
            "-u",
            service_user,
            "--",
            "env",
            "HOME={0}".format(service_home),
            "PUPPETEER_CACHE_DIR={0}".format(cache_dir),
            "npx",
            "--yes",
            "puppeteer",
            "browsers",
            "install",
            "chrome-headless-shell@{0}".format(revision),
        ],
        use_sudo=True,
    )


def ensure_diagram_renderer_dependencies() -> None:
    if not is_linux():
        raise BootstrapError("Diagram renderer setup currently targets Linux VMs only.")

    ensure_node_toolchain()
    if shutil.which("mmdc") is None:
        log(
            "Installing Mermaid CLI globally because the diagram agent depends on mmdc."
        )
        run_with_retry(
            ["npm", "install", "-g", MERMAID_CLI_PACKAGE],
            use_sudo=True,
        )
        if shutil.which("mmdc") is None:
            raise BootstrapError(
                "Mermaid CLI (mmdc) is still unavailable after install."
            )

    service_user = current_service_user()
    service_home = current_service_home()
    cache_dir = DEFAULT_DIAGRAM_PUPPETEER_CACHE_DIR
    run(
        [
            "install",
            "-d",
            "-m",
            "755",
            "-o",
            service_user,
            "-g",
            service_user,
            str(cache_dir),
        ],
        use_sudo=True,
    )

    try:
        _run_mermaid_smoke(
            service_user=service_user,
            service_home=service_home,
            cache_dir=cache_dir,
        )
        log("Verified Mermaid renderer runtime via mmdc smoke test.")
    except subprocess.CalledProcessError as exc:
        combined_output = "\n".join(
            part.strip()
            for part in (exc.stdout or "", exc.stderr or "")
            if part.strip()
        )
        required_revision = _extract_missing_chrome_revision(combined_output)
        if required_revision is None:
            raise BootstrapError(
                "Mermaid renderer smoke failed after bootstrap: {0}".format(
                    combined_output or str(exc)
                )
            ) from exc
        log(
            "Installing Mermaid browser runtime required by mmdc: chrome-headless-shell@{0}".format(
                required_revision
            )
        )
        _install_mermaid_browser_revision(
            service_user=service_user,
            service_home=service_home,
            cache_dir=cache_dir,
            revision=required_revision,
        )
        try:
            _run_mermaid_smoke(
                service_user=service_user,
                service_home=service_home,
                cache_dir=cache_dir,
            )
            log("Verified Mermaid renderer runtime via mmdc smoke test.")
        except subprocess.CalledProcessError as retry_exc:
            retry_output = "\n".join(
                part.strip()
                for part in (retry_exc.stdout or "", retry_exc.stderr or "")
                if part.strip()
            )
            raise BootstrapError(
                "Mermaid renderer still failed after installing the required browser runtime: {0}".format(
                    retry_output or str(retry_exc)
                )
            ) from retry_exc

    if shutil.which("d2") is None:
        log(
            "Warning: d2 is not currently installed. Mermaid and Excalidraw will work, but D2 diagrams will remain unavailable until d2 is installed."
        )


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
        raise BootstrapError(
            "systemctl not found. This host does not appear to use systemd."
        )
    if not template_dir.exists():
        raise BootstrapError(
            "Systemd template directory does not exist: {0}".format(template_dir)
        )

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
        raise BootstrapError(
            "No systemd template files found in {0}".format(template_dir)
        )

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
    run(
        [
            "install",
            "-d",
            "-m",
            "755",
            "-o",
            service_user,
            "-g",
            service_user,
            str(DEFAULT_DIAGRAM_PUPPETEER_CACHE_DIR),
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
                [
                    "install",
                    "-m",
                    "644",
                    str(rendered_path),
                    "/etc/systemd/system/{0}".format(rendered_name),
                ],
                use_sudo=True,
            )
            installed_names.append(rendered_name)

    run(["systemctl", "daemon-reload"], use_sudo=True)

    if enable_units and installed_names:
        preferred_units = [
            name for name in installed_names if name.endswith(".target")
        ] or installed_names
        additional_units = [name for name in (extra_enable_units or []) if name]
        run(["systemctl", "enable", *preferred_units, *additional_units], use_sudo=True)
        if start_units:
            run(
                ["systemctl", "restart", *preferred_units, *additional_units],
                use_sudo=True,
            )

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
    print(
        "  current python     : {0}.{1}.{2} ({3})".format(
            current_version[0], current_version[1], current_version[2], sys.executable
        )
    )
    print("  python supported   : {0}".format("yes" if current_supported else "no"))
    print("  package manager    : {0}".format(manager or "not found"))
    print("  pip available      : {0}".format("yes" if has_pip() else "no"))
    print("  venv available     : {0}".format("yes" if has_venv_module() else "no"))
    print("  target venv        : {0}".format(venv_path))
    print(
        "  venv exists        : {0}".format(
            "yes" if venv_python_path(venv_path).exists() else "no"
        )
    )
    docs_parser_dependency_status = "venv missing"
    if venv_has_pip(venv_path):
        try:
            verify_critical_backend_dependencies(venv_path)
            docs_parser_dependency_status = "ok"
        except BootstrapError as exc:
            docs_parser_dependency_status = "failed: {0}".format(exc)
    elif venv_python_path(venv_path).exists():
        docs_parser_dependency_status = "venv missing pip"
    print("  docs parser deps   : {0}".format(docs_parser_dependency_status))
    print("  office renderer    : {0}".format(office_renderer_version() or "missing"))
    print("  pdf renderer       : {0}".format(pdf_renderer_version() or "missing"))
    if venv_has_pip(venv_path):
        playwright_status = (
            "ok" if playwright_chromium_launchable(venv_path) else "missing"
        )
    else:
        playwright_status = "venv missing"
    print("  playwright chromium: {0}".format(playwright_status))
    print(
        "  requirements file  : {0}".format(
            requirements_path if requirements_path.exists() else "missing"
        )
    )
    print(
        "  node available     : {0}".format(
            executable_version(["node", "--version"]) or "no"
        )
    )
    print(
        "  npm available      : {0}".format(
            executable_version(["npm", "--version"]) or "no"
        )
    )
    print(
        "  codex cli          : {0}".format(
            executable_version(["codex", "--version"]) or "no"
        )
    )
    print(
        "  cursor cli         : {0}".format(
            executable_version(["cursor-agent", "--version"]) or "no"
        )
    )
    print(
        "  bridge dir         : {0}".format(
            bridge_dir if bridge_dir.exists() else "missing"
        )
    )
    print(
        "  bridge package     : {0}".format(
            (bridge_dir / "package.json")
            if (bridge_dir / "package.json").exists()
            else "missing"
        )
    )
    print("  memory repo target : {0}".format(DEFAULT_MEMORY_REPO_DIR))
    print(
        "  memory repo exists : {0}".format(
            "yes" if (DEFAULT_MEMORY_REPO_DIR / "pyproject.toml").exists() else "no"
        )
    )
    firecrawl_source = resolve_firecrawl_agent_env_source()
    firecrawl_source_data = (
        parse_env_text(firecrawl_source.read_text(encoding="utf-8"))
        if firecrawl_source.exists()
        else {}
    )
    firecrawl_system_path = firecrawl_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    firecrawl_system_data = {}
    if is_linux() and firecrawl_system_path.exists():
        firecrawl_system_data = read_firecrawl_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    docs_parser_source = resolve_docs_parser_agent_env_source()
    docs_parser_system_path = docs_parser_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    x_twitter_source = resolve_x_twitter_search_agent_env_source()
    x_twitter_source_data = (
        parse_env_text(x_twitter_source.read_text(encoding="utf-8"))
        if x_twitter_source.exists()
        else {}
    )
    x_twitter_system_path = x_twitter_search_agent_system_env_path(
        DEFAULT_SYSTEM_ENV_DIR
    )
    x_twitter_system_data = {}
    if is_linux() and x_twitter_system_path.exists():
        x_twitter_system_data = read_x_twitter_search_agent_system_env(
            DEFAULT_SYSTEM_ENV_DIR
        )
    email_source = resolve_email_agent_env_source()
    email_source_data = (
        parse_env_text(email_source.read_text(encoding="utf-8"))
        if email_source.exists()
        else {}
    )
    email_system_path = email_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    email_system_data = {}
    if is_linux() and email_system_path.exists():
        email_system_data = read_email_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    image_source = resolve_image_generator_agent_env_source()
    image_source_data = (
        parse_env_text(image_source.read_text(encoding="utf-8"))
        if image_source.exists()
        else {}
    )
    image_system_path = image_generator_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    image_system_data = {}
    if is_linux() and image_system_path.exists():
        image_system_data = read_image_generator_agent_system_env(
            DEFAULT_SYSTEM_ENV_DIR
        )
    diagram_source = resolve_diagram_agent_env_source()
    diagram_source_data = (
        parse_env_text(diagram_source.read_text(encoding="utf-8"))
        if diagram_source.exists()
        else {}
    )
    diagram_system_path = diagram_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    diagram_system_data = {}
    if is_linux() and diagram_system_path.exists():
        diagram_system_data = read_diagram_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    slide_source = resolve_slide_agent_env_source()
    slide_source_data = (
        parse_env_text(slide_source.read_text(encoding="utf-8"))
        if slide_source.exists()
        else {}
    )
    slide_system_path = slide_agent_system_env_path(DEFAULT_SYSTEM_ENV_DIR)
    slide_system_data = {}
    if is_linux() and slide_system_path.exists():
        slide_system_data = read_slide_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    print(
        "  firecrawl env src  : {0}".format(
            firecrawl_source if firecrawl_source.exists() else "missing"
        )
    )
    print(
        "  docs parser env src: {0}".format(
            docs_parser_source if docs_parser_source.exists() else "missing"
        )
    )
    print(
        "  x search env src   : {0}".format(
            x_twitter_source if x_twitter_source.exists() else "missing"
        )
    )
    print(
        "  email agent env src: {0}".format(
            email_source if email_source.exists() else "missing"
        )
    )
    print(
        "  image agent env src: {0}".format(
            image_source if image_source.exists() else "missing"
        )
    )
    print(
        "  diagram agent env src: {0}".format(
            diagram_source if diagram_source.exists() else "missing"
        )
    )
    print(
        "  slide agent env src: {0}".format(
            slide_source if slide_source.exists() else "missing"
        )
    )
    if is_linux():
        print(
            "  firecrawl system env: {0}".format(
                firecrawl_system_path if firecrawl_system_path.exists() else "missing"
            )
        )
        print(
            "  docs parser system env: {0}".format(
                docs_parser_system_path
                if docs_parser_system_path.exists()
                else "missing"
            )
        )
        print(
            "  x search system env: {0}".format(
                x_twitter_system_path if x_twitter_system_path.exists() else "missing"
            )
        )
        print(
            "  email agent system env: {0}".format(
                email_system_path if email_system_path.exists() else "missing"
            )
        )
        print(
            "  image agent system env: {0}".format(
                image_system_path if image_system_path.exists() else "missing"
            )
        )
        print(
            "  diagram agent system env: {0}".format(
                diagram_system_path if diagram_system_path.exists() else "missing"
            )
        )
        print(
            "  slide agent system env: {0}".format(
                slide_system_path if slide_system_path.exists() else "missing"
            )
        )
    firecrawl_enabled = firecrawl_agent_is_configured(
        firecrawl_system_data if firecrawl_system_data else firecrawl_source_data
    )
    x_twitter_enabled = x_twitter_search_agent_is_configured(
        x_twitter_system_data if x_twitter_system_data else x_twitter_source_data
    )
    email_enabled = email_agent_enabled_via_env_or_integration(
        email_system_data if email_system_data else email_source_data
    )
    image_enabled = image_generator_agent_is_configured(
        image_system_data if image_system_data else image_source_data
    )
    diagram_enabled = diagram_agent_is_configured(
        diagram_system_data if diagram_system_data else diagram_source_data
    )
    slide_enabled = slide_agent_is_configured(
        slide_system_data if slide_system_data else slide_source_data
    )
    print("  firecrawl enabled  : {0}".format("yes" if firecrawl_enabled else "no"))
    print("  x search enabled   : {0}".format("yes" if x_twitter_enabled else "no"))
    print("  email agent enabled: {0}".format("yes" if email_enabled else "no"))
    print("  image agent enabled: {0}".format("yes" if image_enabled else "no"))
    print("  diagram agent enabled: {0}".format("yes" if diagram_enabled else "no"))
    print("  slide agent enabled: {0}".format("yes" if slide_enabled else "no"))
    if is_linux() and shutil.which("systemctl") is not None:
        neo4j_status = run(
            ["systemctl", "is-active", DEFAULT_NEO4J_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  neo4j service      : {0}".format(
                (neo4j_status.stdout or "unknown").strip() or "unknown"
            )
        )
        firecrawl_status = run(
            ["systemctl", "is-active", FIRECRAWL_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  firecrawl service  : {0}".format(
                (firecrawl_status.stdout or "unknown").strip() or "unknown"
            )
        )
        docs_parser_status = run(
            ["systemctl", "is-active", DOCS_PARSER_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  docs parser service: {0}".format(
                (docs_parser_status.stdout or "unknown").strip() or "unknown"
            )
        )
        x_twitter_status = run(
            ["systemctl", "is-active", X_TWITTER_SEARCH_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  x search service   : {0}".format(
                (x_twitter_status.stdout or "unknown").strip() or "unknown"
            )
        )
        email_status = run(
            ["systemctl", "is-active", EMAIL_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  email agent service: {0}".format(
                (email_status.stdout or "unknown").strip() or "unknown"
            )
        )
        image_status = run(
            ["systemctl", "is-active", IMAGE_GENERATOR_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  image agent service: {0}".format(
                (image_status.stdout or "unknown").strip() or "unknown"
            )
        )
        diagram_status = run(
            ["systemctl", "is-active", DIAGRAM_AGENT_SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        print(
            "  diagram agent service: {0}".format(
                (diagram_status.stdout or "unknown").strip() or "unknown"
            )
        )
    print(
        "  env search roots   : {0}".format(
            ", ".join(str(path) for path in env_search_roots)
        )
    )
    print("  env templates      : {0}".format(len(env_examples)))
    print(
        "  systemd templates  : {0}".format(
            systemd_template_dir if systemd_template_dir.exists() else "missing"
        )
    )

    effective_sources: list[Tuple[Path, Path]] = []
    fallback_sources = {
        dest: source
        for source, dest in fallback_service_env_specs(DEFAULT_SYSTEM_ENV_DIR)
    }
    for source, dest in service_env_specs(DEFAULT_SYSTEM_ENV_DIR):
        effective_sources.append(
            (source if source.exists() else fallback_sources[dest], dest)
        )

    for source_path, dest_path in effective_sources:
        required_keys = REQUIRED_SERVICE_ENV_KEYS.get(dest_path.name, ())
        if not required_keys:
            continue
        missing_keys = missing_required_env_keys(source_path, required_keys)
        print(
            "  required env check : {0} -> {1}".format(
                source_path,
                "ok"
                if not missing_keys
                else "missing {0}".format(", ".join(missing_keys)),
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
        changed_keys = sync_env_file(
            target_path,
            source_raw=source_raw,
            create_missing=True,
            use_sudo=False,
            mode="644",
        )
        if changed_keys or target_path.exists():
            synced.append(target_path)
    return synced


def sync_service_env_files(
    system_env_dir: Path, *, include_memory: bool = False
) -> List[Path]:
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
        existing_env_by_name[dest_path.name] = parse_env_text(
            read_text_file(dest_path, use_sudo=True)
        )

    overrides_by_dest = build_service_env_overrides(
        effective_sources,
        include_memory=include_memory,
        existing_env_by_name=existing_env_by_name,
    )

    synced: list[Path] = []
    for source_path, dest_path in effective_sources:
        raw = source_path.read_text(encoding="utf-8")
        rendered = render_env_with_overrides(
            raw, overrides_by_dest.get(dest_path.name, {})
        )
        changed_keys = sync_env_file(
            dest_path,
            source_raw=rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(dest_path)

    visual_dest_path, visual_rendered, _visual_env = build_visual_enhancement_env_rendered(
        system_env_dir=system_env_dir,
        existing_env_by_name=existing_env_by_name,
    )
    changed_keys = sync_env_file(
        visual_dest_path,
        source_raw=visual_rendered,
        create_missing=True,
        use_sudo=True,
        mode="600",
    )
    if changed_keys:
        synced.append(visual_dest_path)

    firecrawl_dest_path = firecrawl_agent_system_env_path(system_env_dir)
    if firecrawl_dest_path.exists():
        firecrawl_existing_by_name: Dict[str, Dict[str, str]] = {
            FIRECRAWL_AGENT_ENV_NAME: parse_env_text(
                read_text_file(firecrawl_dest_path, use_sudo=True)
            ),
        }
        _firecrawl_dest_path, firecrawl_rendered, _firecrawl_env = (
            build_firecrawl_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=firecrawl_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            firecrawl_dest_path,
            source_raw=firecrawl_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(firecrawl_dest_path)

    docs_parser_dest_path = docs_parser_agent_system_env_path(system_env_dir)
    if docs_parser_dest_path.exists():
        docs_parser_existing_by_name: Dict[str, Dict[str, str]] = {
            DOCS_PARSER_AGENT_ENV_NAME: parse_env_text(
                read_text_file(docs_parser_dest_path, use_sudo=True)
            ),
        }
        _docs_parser_dest_path, docs_parser_rendered, _docs_parser_env = (
            build_docs_parser_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=docs_parser_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            docs_parser_dest_path,
            source_raw=docs_parser_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(docs_parser_dest_path)
    tabular_dest_path = tabular_agent_system_env_path(system_env_dir)
    if tabular_dest_path.exists():
        tabular_existing_by_name: Dict[str, Dict[str, str]] = {
            TABULAR_AGENT_ENV_NAME: parse_env_text(
                read_text_file(tabular_dest_path, use_sudo=True)
            ),
        }
        _tabular_dest_path, tabular_rendered, _tabular_env = (
            build_tabular_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=tabular_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            tabular_dest_path,
            source_raw=tabular_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(tabular_dest_path)
    email_dest_path = email_agent_system_env_path(system_env_dir)
    if email_dest_path.exists():
        email_existing_by_name: Dict[str, Dict[str, str]] = {
            EMAIL_AGENT_ENV_NAME: parse_env_text(
                read_text_file(email_dest_path, use_sudo=True)
            ),
        }
        _email_dest_path, email_rendered, _email_env = build_email_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=email_existing_by_name,
        )
        changed_keys = sync_env_file(
            email_dest_path,
            source_raw=email_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(email_dest_path)
    image_dest_path = image_generator_agent_system_env_path(system_env_dir)
    if image_dest_path.exists():
        image_existing_by_name: Dict[str, Dict[str, str]] = {
            IMAGE_GENERATOR_AGENT_ENV_NAME: parse_env_text(
                read_text_file(image_dest_path, use_sudo=True)
            ),
        }
        _image_dest_path, image_rendered, _image_env = (
            build_image_generator_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=image_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            image_dest_path,
            source_raw=image_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(image_dest_path)
    calendar_dest_path = calendar_agent_system_env_path(system_env_dir)
    if calendar_dest_path.exists():
        calendar_existing_by_name: Dict[str, Dict[str, str]] = {
            CALENDAR_AGENT_ENV_NAME: parse_env_text(
                read_text_file(calendar_dest_path, use_sudo=True)
            ),
        }
        _calendar_dest_path, calendar_rendered, _calendar_env = (
            build_calendar_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=calendar_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            calendar_dest_path,
            source_raw=calendar_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(calendar_dest_path)
    gmail_dest_path = gmail_agent_system_env_path(system_env_dir)
    if gmail_dest_path.exists():
        gmail_existing_by_name: Dict[str, Dict[str, str]] = {
            GMAIL_AGENT_ENV_NAME: parse_env_text(
                read_text_file(gmail_dest_path, use_sudo=True)
            ),
        }
        _gmail_dest_path, gmail_rendered, _gmail_env = build_gmail_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"][
                "GATEWAY_SIGNING_SECRET"
            ],
            shared_internal_token=overrides_by_dest["gateway.env"][
                "GATEWAY_INTERNAL_TOKEN"
            ],
            system_env_dir=system_env_dir,
            existing_env_by_name=gmail_existing_by_name,
            external_env_by_name={
                GMAIL_AGENT_ENV_NAME: {
                    "GMAIL_WEBHOOK_SECRET": overrides_by_dest.get("gateway.env", {}).get(
                        "GATEWAY_GMAIL_WEBHOOK_SECRET", ""
                    )
                }
            },
        )
        changed_keys = sync_env_file(
            gmail_dest_path,
            source_raw=gmail_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(gmail_dest_path)
    google_docs_dest_path = google_docs_agent_system_env_path(system_env_dir)
    if google_docs_dest_path.exists():
        google_docs_existing_by_name: Dict[str, Dict[str, str]] = {
            GOOGLE_DOCS_AGENT_ENV_NAME: parse_env_text(
                read_text_file(google_docs_dest_path, use_sudo=True)
            ),
        }
        _google_docs_dest_path, google_docs_rendered, _google_docs_env = (
            build_google_docs_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=google_docs_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            google_docs_dest_path,
            source_raw=google_docs_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(google_docs_dest_path)
    google_sheets_dest_path = google_sheets_agent_system_env_path(system_env_dir)
    if google_sheets_dest_path.exists():
        google_sheets_existing_by_name: Dict[str, Dict[str, str]] = {
            GOOGLE_SHEETS_AGENT_ENV_NAME: parse_env_text(
                read_text_file(google_sheets_dest_path, use_sudo=True)
            ),
        }
        _google_sheets_dest_path, google_sheets_rendered, _google_sheets_env = (
            build_google_sheets_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=google_sheets_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            google_sheets_dest_path,
            source_raw=google_sheets_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(google_sheets_dest_path)
    diagram_dest_path = diagram_agent_system_env_path(system_env_dir)
    if diagram_dest_path.exists():
        diagram_existing_by_name: Dict[str, Dict[str, str]] = {
            DIAGRAM_AGENT_ENV_NAME: parse_env_text(
                read_text_file(diagram_dest_path, use_sudo=True)
            ),
        }
        _diagram_dest_path, diagram_rendered, _diagram_env = (
            build_diagram_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=diagram_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            diagram_dest_path,
            source_raw=diagram_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(diagram_dest_path)
    map_dest_path = map_agent_system_env_path(system_env_dir)
    if map_dest_path.exists():
        map_existing_by_name: Dict[str, Dict[str, str]] = {
            MAP_AGENT_ENV_NAME: parse_env_text(
                read_text_file(map_dest_path, use_sudo=True)
            ),
        }
        _map_dest_path, map_rendered, _map_env = build_map_agent_env_rendered(
            signing_secret=overrides_by_dest["gateway.env"]["GATEWAY_SIGNING_SECRET"],
            shared_internal_token=overrides_by_dest["gateway.env"]["GATEWAY_INTERNAL_TOKEN"],
            system_env_dir=system_env_dir,
            existing_env_by_name=map_existing_by_name,
        )
        changed_keys = sync_env_file(
            map_dest_path,
            source_raw=map_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(map_dest_path)
    slide_dest_path = slide_agent_system_env_path(system_env_dir)
    if slide_dest_path.exists():
        slide_existing_by_name: Dict[str, Dict[str, str]] = {
            SLIDE_AGENT_ENV_NAME: parse_env_text(
                read_text_file(slide_dest_path, use_sudo=True)
            ),
        }
        _slide_dest_path, slide_rendered, _slide_env = (
            build_slide_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=slide_existing_by_name,
            )
        )
        changed_keys = sync_env_file(
            slide_dest_path,
            source_raw=slide_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(slide_dest_path)
    alpha_dest_path = alpha_agent_system_env_path(system_env_dir)
    if alpha_dest_path.exists():
        alpha_existing_by_name: Dict[str, Dict[str, str]] = {
            ALPHA_AGENT_ENV_NAME: parse_env_text(
                read_text_file(alpha_dest_path, use_sudo=True)
            ),
        }
        _alpha_dest_path, alpha_rendered, _alpha_env = (
            build_alpha_agent_env_rendered(
                signing_secret=overrides_by_dest["gateway.env"][
                    "GATEWAY_SIGNING_SECRET"
                ],
                shared_internal_token=overrides_by_dest["gateway.env"][
                    "GATEWAY_INTERNAL_TOKEN"
                ],
                system_env_dir=system_env_dir,
                existing_env_by_name=alpha_existing_by_name,
                gateway_public_host=overrides_by_dest["gateway.env"].get(
                    "GATEWAY_PUBLIC_HOST"
                ),
            )
        )
        changed_keys = sync_env_file(
            alpha_dest_path,
            source_raw=alpha_rendered,
            create_missing=False,
            use_sudo=True,
            mode="600",
        )
        if changed_keys:
            synced.append(alpha_dest_path)
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

    ensure_office_renderer()
    ensure_pdf_renderer()
    ensure_slide_python_build_dependencies()
    ensure_python3_available()
    ensure_pip()
    ensure_venv_support()
    ensure_virtualenv(venv_path)
    upgrade_venv_pip(venv_path)
    install_python_requirements(venv_path, requirements_path)
    verify_critical_backend_dependencies(venv_path)
    ensure_playwright_chromium(venv_path)


def setup_whatsapp_bridge(bridge_dir: Path) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    install_whatsapp_bridge_dependencies(bridge_dir)


def setup_diagram_renderers() -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    ensure_diagram_renderer_dependencies()


def resolve_memory_repo_dir(
    configured_path: Path | None, *, allow_missing: bool = False
) -> Path | None:
    if configured_path is None:
        return None
    resolved = configured_path.expanduser().resolve()
    if not resolved.exists():
        if allow_missing:
            return resolved
        raise BootstrapError(
            "cosmic-memory repo directory does not exist: {0}".format(resolved)
        )
    if not (resolved / "pyproject.toml").exists():
        raise BootstrapError(
            "cosmic-memory repo is missing pyproject.toml: {0}".format(resolved)
        )
    return resolved


def ensure_git_available() -> None:
    if shutil.which("git"):
        return
    if not is_linux():
        raise BootstrapError("git is required to fetch cosmic-memory.")
    manager = detect_package_manager()
    if not manager:
        raise BootstrapError(
            "git is required to fetch cosmic-memory, but no supported package manager was found."
        )
    install_system_packages(manager, ["git"])
    if shutil.which("git") is None:
        raise BootstrapError("git is still unavailable after installation attempts.")


def ensure_memory_repo_checkout(
    memory_repo_dir: Path, memory_repo_url: str, memory_repo_ref: str
) -> Path:
    resolved = resolve_memory_repo_dir(memory_repo_dir, allow_missing=True)
    if resolved is None:
        raise BootstrapError("cosmic-memory repo path could not be resolved.")
    if resolved.exists():
        if not resolved.is_dir():
            raise BootstrapError(
                "cosmic-memory repo path is not a directory: {0}".format(resolved)
            )
        if (resolved / "pyproject.toml").exists():
            return resolve_memory_repo_dir(resolved)
        if any(resolved.iterdir()):
            raise BootstrapError(
                "cosmic-memory checkout target exists but is not a valid repo: {0}".format(
                    resolved
                )
            )
    ensure_git_available()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    clone_command = ["git", "clone", "--depth", "1"]
    normalized_ref = meaningful_env_value(memory_repo_ref)
    if normalized_ref is not None:
        clone_command.extend(["--branch", normalized_ref])
    clone_command.extend([memory_repo_url, str(resolved)])
    run_with_retry(clone_command)
    return resolve_memory_repo_dir(resolved)


def apt_package_installed(package_name: str) -> bool:
    result = run(
        ["dpkg-query", "-W", "-f=${Status}", package_name],
        capture_output=True,
        check=False,
    )
    status = (result.stdout or "").strip().lower()
    return result.returncode == 0 and "install ok installed" in status


def apt_has_candidate(package_name: str) -> bool:
    result = run(
        ["apt-cache", "policy", package_name],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    output = (result.stdout or "").strip().lower()
    return "candidate: (none)" not in output


def ensure_neo4j_apt_repository() -> None:
    manager = detect_package_manager()
    if manager != "apt-get":
        raise BootstrapError(
            "Automatic Neo4j provisioning currently supports apt-get based Linux VMs only."
        )

    install_system_packages(manager, ["ca-certificates", "gpg"])
    if is_ubuntu_host() and not apt_has_candidate("daemon"):
        install_system_packages(manager, ["software-properties-common"])
        run_with_retry(["add-apt-repository", "-y", "universe"], use_sudo=True)

    run(
        ["install", "-d", "-m", "755", str(DEFAULT_NEO4J_APT_KEYRING_PATH.parent)],
        use_sudo=True,
    )
    request = Request(
        DEFAULT_NEO4J_APT_KEY_URL,
        headers={"User-Agent": "cosmic-bootstrap/1.0"},
    )
    with retry_call(
        "Downloading Neo4j APT signing key",
        lambda: urlopen(request, timeout=30),
        retry_exceptions=(HTTPError, URLError),
        should_retry=should_retry_bootstrap_http_error,
    ) as response:
        key_bytes = response.read()

    with tempfile.TemporaryDirectory(prefix="cosmic-neo4j-key-") as temp_dir:
        temp_dir_path = Path(temp_dir)
        raw_key_path = temp_dir_path / "neo4j.asc"
        dearmored_key_path = temp_dir_path / "neo4j.gpg"
        raw_key_path.write_bytes(key_bytes)
        run(
            [
                "gpg",
                "--dearmor",
                "--yes",
                "--output",
                str(dearmored_key_path),
                str(raw_key_path),
            ],
            capture_output=False,
            check=True,
        )
        install_bytes_file(
            DEFAULT_NEO4J_APT_KEYRING_PATH,
            dearmored_key_path.read_bytes(),
            mode="644",
            use_sudo=True,
        )

    install_text_file(
        DEFAULT_NEO4J_APT_SOURCE_PATH,
        DEFAULT_NEO4J_APT_SOURCE + "\n",
        mode="644",
        use_sudo=True,
    )
    run_with_retry(["apt-get", "update"], use_sudo=True)


def ensure_neo4j_package_installed() -> bool:
    ensure_neo4j_apt_repository()
    if apt_package_installed("neo4j"):
        return False
    run_with_retry(
        ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "neo4j"],
        use_sudo=True,
    )
    return True


def wait_for_tcp_endpoint(uri: str, *, timeout_sec: float = 45.0) -> None:
    parsed = urlparse(uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 7687
    deadline = time.time() + timeout_sec
    last_error: Optional[OSError] = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1.0)
    raise BootstrapError(
        "Timed out waiting for Neo4j at {0}:{1}: {2}".format(
            host,
            port,
            last_error or "unreachable",
        )
    )


def neo4j_auth_works(uri: str, username: str, password: str) -> bool:
    try:
        run_redacted(
            [
                "cypher-shell",
                "-a",
                uri,
                "-u",
                username,
                "-p",
                password,
                "--non-interactive",
                "RETURN 1;",
            ],
            display_command=[
                "cypher-shell",
                "-a",
                uri,
                "-u",
                username,
                "-p",
                "<redacted>",
                "--non-interactive",
                "RETURN 1;",
            ],
            capture_output=True,
        )
        return True
    except (BootstrapError, FileNotFoundError, subprocess.CalledProcessError):
        return False


def set_neo4j_initial_password(password: str) -> None:
    run_redacted(
        ["neo4j-admin", "dbms", "set-initial-password", password],
        display_command=["neo4j-admin", "dbms", "set-initial-password", "<redacted>"],
        use_sudo=True,
    )


def rotate_neo4j_password(
    uri: str, username: str, current_password: str, new_password: str
) -> None:
    query = "ALTER CURRENT USER SET PASSWORD FROM '{0}' TO '{1}'".format(
        current_password,
        new_password,
    )
    run_redacted(
        [
            "cypher-shell",
            "-a",
            uri,
            "-u",
            username,
            "-p",
            current_password,
            "--non-interactive",
            query,
        ],
        display_command=[
            "cypher-shell",
            "-a",
            uri,
            "-u",
            username,
            "-p",
            "<redacted>",
            "--non-interactive",
            "ALTER CURRENT USER SET PASSWORD FROM '<redacted>' TO '<redacted>'",
        ],
        capture_output=True,
    )


def setup_neo4j(memory_env_path: Path) -> None:
    if not is_linux():
        raise BootstrapError(
            "Automatic Neo4j provisioning currently targets Linux VMs only."
        )

    env_data = parse_env_text(read_text_file(memory_env_path, use_sudo=True))
    graph_backend = (
        (
            first_meaningful_value(env_data.get("COSMIC_MEMORY_GRAPH_BACKEND"), "neo4j")
            or ""
        )
        .strip()
        .lower()
    )
    if graph_backend != "neo4j":
        log(
            "Skipping Neo4j provisioning because COSMIC_MEMORY_GRAPH_BACKEND={0}.".format(
                graph_backend or "<empty>"
            )
        )
        return

    neo4j_uri = (
        first_meaningful_value(
            env_data.get("COSMIC_MEMORY_NEO4J_URI"), DEFAULT_NEO4J_URI
        )
        or DEFAULT_NEO4J_URI
    )
    neo4j_username = (
        first_meaningful_value(
            env_data.get("COSMIC_MEMORY_NEO4J_USERNAME"), DEFAULT_NEO4J_USERNAME
        )
        or DEFAULT_NEO4J_USERNAME
    )
    neo4j_password = meaningful_env_value(env_data.get("COSMIC_MEMORY_NEO4J_PASSWORD"))
    if neo4j_password is None:
        raise BootstrapError(
            "memory.env is configured for Neo4j but COSMIC_MEMORY_NEO4J_PASSWORD is blank or still a placeholder."
        )

    freshly_installed = ensure_neo4j_package_installed()
    sync_assignment_file(
        DEFAULT_NEO4J_CONFIG_PATH,
        overrides={"server.default_listen_address": "127.0.0.1"},
        create_missing=False,
        use_sudo=True,
        mode="644",
    )

    if freshly_installed:
        try:
            set_neo4j_initial_password(neo4j_password)
        except subprocess.CalledProcessError:
            log(
                "Neo4j initial password setup was not accepted before first start; will verify or rotate after service startup."
            )

    if shutil.which("systemctl") is None:
        raise BootstrapError(
            "systemctl not found. Neo4j provisioning expects a systemd-based Linux VM."
        )

    run(["systemctl", "enable", DEFAULT_NEO4J_SERVICE_NAME], use_sudo=True)
    run(["systemctl", "restart", DEFAULT_NEO4J_SERVICE_NAME], use_sudo=True)
    wait_for_tcp_endpoint(neo4j_uri)

    if neo4j_auth_works(neo4j_uri, neo4j_username, neo4j_password):
        return

    if neo4j_auth_works(neo4j_uri, neo4j_username, "neo4j"):
        rotate_neo4j_password(neo4j_uri, neo4j_username, "neo4j", neo4j_password)
        if neo4j_auth_works(neo4j_uri, neo4j_username, neo4j_password):
            return

    raise BootstrapError(
        "Neo4j is installed, but COSMIC_MEMORY_NEO4J_PASSWORD does not authenticate. "
        "Refusing to continue with an unknown graph DB password state."
    )


def systemd_unit_state(unit_name: str) -> str:
    result = run(
        ["systemctl", "is-active", unit_name],
        capture_output=True,
        check=False,
    )
    return (result.stdout or "").strip().lower() or "unknown"


def wait_for_systemd_unit_active(
    unit_name: str,
    *,
    timeout_sec: float = DEFAULT_POST_PROVISION_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POST_PROVISION_POLL_INTERVAL_SEC,
) -> None:
    deadline = time.time() + max(1.0, timeout_sec)
    last_state = "unknown"
    while time.time() < deadline:
        last_state = systemd_unit_state(unit_name)
        if last_state == "active":
            return
        if last_state in {"failed", "inactive"}:
            time.sleep(min(poll_interval_sec, 1.0))
        else:
            time.sleep(poll_interval_sec)
    raise BootstrapError(
        "Timed out waiting for systemd unit {0} to become active (last state: {1}).".format(
            unit_name,
            last_state,
        )
    )


def fetch_json(url: str, *, timeout_sec: float = 5.0) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "User-Agent": "cosmic-bootstrap/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout_sec) as response:
        raw = response.read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(
            "Health endpoint returned invalid JSON for {0}: {1}".format(url, exc)
        ) from exc
    if not isinstance(parsed, dict):
        raise BootstrapError(
            "Health endpoint returned unexpected payload for {0}.".format(url)
        )
    return parsed


def health_payload_ready(check_name: str, payload: dict[str, object]) -> bool:
    status_value = str(payload.get("status") or "").strip().lower()
    ok_value = payload.get("ok")
    if check_name == "gateway":
        return status_value == "ready"
    if check_name == "orchestrator":
        return status_value == "ok"
    if check_name == "memory":
        if isinstance(ok_value, bool):
            return ok_value
        return status_value in {"ok", "ready"}
    return status_value in {"ok", "ready"}


def wait_for_health_endpoint(
    url: str,
    *,
    check_name: str,
    timeout_sec: float = DEFAULT_POST_PROVISION_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POST_PROVISION_POLL_INTERVAL_SEC,
) -> dict[str, object]:
    deadline = time.time() + max(1.0, timeout_sec)
    last_payload: dict[str, object] | None = None
    last_error: str | None = None
    while time.time() < deadline:
        try:
            payload = fetch_json(url)
            last_payload = payload
            if health_payload_ready(check_name, payload):
                return payload
            last_error = "not ready"
        except (BootstrapError, HTTPError, URLError) as exc:
            last_error = str(exc)
        time.sleep(poll_interval_sec)
    if last_payload is not None:
        raise BootstrapError(
            "Timed out waiting for {0} health at {1}. Last payload: {2}".format(
                check_name,
                url,
                json.dumps(last_payload, sort_keys=True),
            )
        )
    raise BootstrapError(
        "Timed out waiting for {0} health at {1}. Last error: {2}".format(
            check_name,
            url,
            last_error or "unknown error",
        )
    )


def orchestrator_agent_ready(payload: dict[str, object], *, agent_id: str) -> bool:
    if not health_payload_ready("orchestrator", payload):
        return False
    agent_dispatch = payload.get("agent_dispatch")
    if not isinstance(agent_dispatch, dict):
        return False
    agents = agent_dispatch.get("agents")
    if not isinstance(agents, list):
        return False
    normalized_agent_id = str(agent_id or "").strip()
    for item in agents:
        if not isinstance(item, dict):
            continue
        if str(item.get("agent_id") or "").strip() != normalized_agent_id:
            continue
        return bool(item.get("healthy_instance"))
    return False


def wait_for_orchestrator_agent_ready(
    agent_id: str,
    *,
    timeout_sec: float = DEFAULT_POST_PROVISION_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POST_PROVISION_POLL_INTERVAL_SEC,
) -> dict[str, object]:
    deadline = time.time() + max(1.0, timeout_sec)
    last_payload: dict[str, object] | None = None
    while time.time() < deadline:
        payload = fetch_json("http://127.0.0.1:8743/health")
        last_payload = payload
        if orchestrator_agent_ready(payload, agent_id=agent_id):
            return payload
        time.sleep(poll_interval_sec)
    raise BootstrapError(
        "Timed out waiting for orchestrator to report agent {0} healthy. Last payload: {1}".format(
            agent_id,
            json.dumps(last_payload or {}, sort_keys=True),
        )
    )


def run_post_provision_health_checks(
    *,
    include_memory: bool,
    include_firecrawl_agent: bool = False,
    include_x_twitter_search_agent: bool = False,
    include_tabular_agent: bool = True,
    include_email_agent: bool = False,
    include_gmail_agent: bool = False,
    include_google_docs_agent: bool = False,
    include_google_sheets_agent: bool = False,
    include_image_generator_agent: bool = False,
    include_diagram_agent: bool = False,
    include_map_agent: bool = False,
    include_slide_agent: bool = False,
    include_alpha_agent: bool = False,
    timeout_sec: float = DEFAULT_POST_PROVISION_TIMEOUT_SEC,
    poll_interval_sec: float = DEFAULT_POST_PROVISION_POLL_INTERVAL_SEC,
) -> None:
    if not is_linux():
        raise BootstrapError(
            "Post-provision readiness checks currently target Linux VMs only."
        )
    if shutil.which("systemctl") is None:
        raise BootstrapError(
            "systemctl not found. Post-provision readiness checks require a systemd-based Linux VM."
        )

    for unit_name in CORE_BACKEND_SERVICE_UNITS:
        wait_for_systemd_unit_active(
            unit_name,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    wait_for_health_endpoint(
        "http://127.0.0.1:8743/health",
        check_name="orchestrator",
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )

    if include_memory:
        wait_for_systemd_unit_active(
            "cosmic-memory.service",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_health_endpoint(
            "http://127.0.0.1:8090/health",
            check_name="memory",
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_firecrawl_agent:
        wait_for_systemd_unit_active(
            FIRECRAWL_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            FIRECRAWL_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_x_twitter_search_agent:
        wait_for_systemd_unit_active(
            X_TWITTER_SEARCH_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            X_TWITTER_SEARCH_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_tabular_agent:
        wait_for_systemd_unit_active(
            TABULAR_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            TABULAR_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_email_agent:
        wait_for_systemd_unit_active(
            EMAIL_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            EMAIL_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_gmail_agent:
        wait_for_systemd_unit_active(
            GMAIL_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            GMAIL_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_google_docs_agent:
        wait_for_systemd_unit_active(
            GOOGLE_DOCS_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            GOOGLE_DOCS_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_google_sheets_agent:
        wait_for_systemd_unit_active(
            GOOGLE_SHEETS_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            GOOGLE_SHEETS_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_image_generator_agent:
        wait_for_systemd_unit_active(
            IMAGE_GENERATOR_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            IMAGE_GENERATOR_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_diagram_agent:
        wait_for_systemd_unit_active(
            DIAGRAM_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            DIAGRAM_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_map_agent:
        wait_for_systemd_unit_active(
            MAP_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            MAP_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_slide_agent:
        wait_for_systemd_unit_active(
            SLIDE_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            SLIDE_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    if include_alpha_agent:
        wait_for_systemd_unit_active(
            ALPHA_AGENT_SERVICE_NAME,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )
        wait_for_orchestrator_agent_ready(
            ALPHA_AGENT_ID,
            timeout_sec=timeout_sec,
            poll_interval_sec=poll_interval_sec,
        )

    wait_for_health_endpoint(
        "http://127.0.0.1:8080/health/ready",
        check_name="gateway",
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )

    log("Post-provision readiness checks passed.")


def setup_cosmic_memory(
    venv_path: Path,
    memory_repo_dir: Path,
    *,
    memory_repo_url: str,
    memory_repo_ref: str,
) -> None:
    if not is_linux():
        raise BootstrapError("This bootstrap flow currently targets Linux VMs only.")

    python_path = venv_python_path(venv_path)
    if not python_path.exists():
        raise BootstrapError(
            "Missing venv python executable at {0}".format(python_path)
        )

    memory_repo = ensure_memory_repo_checkout(
        memory_repo_dir, memory_repo_url, memory_repo_ref
    )

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
        raise BootstrapError(
            "VM edge setup script does not exist: {0}".format(edge_setup_script)
        )

    command = [
        sys.executable,
        str(edge_setup_script),
        "--gateway-env",
        str(gateway_env_path),
    ]
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
    memory_repo_url: str = DEFAULT_MEMORY_REPO_URL,
    memory_repo_ref: str = DEFAULT_MEMORY_REPO_REF,
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
        setup_cosmic_memory(
            venv_path,
            memory_repo_dir,
            memory_repo_url=memory_repo_url,
            memory_repo_ref=memory_repo_ref,
        )
    setup_whatsapp_bridge(bridge_dir)
    setup_diagram_renderers()
    ensure_openai_codex_cli()
    ensure_cursor_cli()
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
    if enable_memory and memory_repo_dir is not None:
        print("  memory : {0}".format(memory_repo_dir))
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
    memory_repo_url: str = DEFAULT_MEMORY_REPO_URL,
    memory_repo_ref: str = DEFAULT_MEMORY_REPO_REF,
) -> None:
    enable_memory = memory_repo_dir is not None
    enable_firecrawl_agent = False
    enable_tabular_agent = True
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
        memory_repo_url=memory_repo_url,
        memory_repo_ref=memory_repo_ref,
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
        install_service_env_files(DEFAULT_SYSTEM_ENV_DIR, include_memory=True)
        setup_neo4j(DEFAULT_SYSTEM_ENV_DIR / "memory.env")
    else:
        install_service_env_files(DEFAULT_SYSTEM_ENV_DIR, include_memory=False)
    firecrawl_env = read_firecrawl_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_firecrawl_agent = firecrawl_agent_is_configured(firecrawl_env)
    if enable_firecrawl_agent:
        log(
            "Firecrawl agent env is configured; bootstrap will enable and start the Firecrawl agent service."
        )
    else:
        log(
            "Firecrawl agent env is not configured; bootstrap will install the unit but skip enabling the agent service."
        )
    x_twitter_env = read_x_twitter_search_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_x_twitter_search_agent = x_twitter_search_agent_is_configured(x_twitter_env)
    if enable_x_twitter_search_agent:
        log(
            "X/Twitter search agent env is configured; bootstrap will enable and start the X/Twitter search agent service."
        )
    else:
        log(
            "X/Twitter search agent env is not configured; bootstrap will install the unit but skip enabling the agent service."
        )
    tabular_env = read_tabular_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    if meaningful_env_value(tabular_env.get("TABULAR_AGENT_INTERNAL_LLM_API_KEY")) is not None:
        log(
            "Tabular agent env includes internal LLM credentials; bootstrap will enable and start the tabular agent service with internal LLM support."
        )
    else:
        log(
            "Tabular agent env does not include internal LLM credentials; bootstrap will still enable and start the tabular agent service for deterministic spreadsheet work."
        )
    email_env = read_email_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_email_agent = email_agent_enabled_via_env_or_integration(email_env)
    if enable_email_agent:
        if meaningful_env_value(email_env.get("EMAIL_AGENT_INTERNAL_LLM_API_KEY")) is not None:
            log(
                "Email agent env is configured with Cosmic Mail + internal LLM credentials; bootstrap will enable and start the email agent service."
            )
        else:
            if email_agent_is_configured(email_env):
                log(
                    "Email agent env is configured with Cosmic Mail credentials but no internal LLM key; bootstrap will still enable and start the email agent service."
                )
            else:
                log(
                    "Agent Email is configured through the shared backend integration store; bootstrap will enable and start the email agent service."
                )
    else:
        log(
            "Email agent env is not configured; bootstrap will install the unit but skip enabling the email agent service."
        )
    gmail_env = read_gmail_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_gmail_agent = gmail_agent_is_configured(gmail_env)
    if enable_gmail_agent:
        if meaningful_env_value(gmail_env.get("GMAIL_AGENT_INTERNAL_LLM_API_KEY")) is not None:
            log(
                "Gmail agent env is configured with internal LLM credentials; bootstrap will enable and start the Gmail agent service."
            )
        else:
            log(
                "Gmail agent env is enabled without an internal LLM key; bootstrap will still start the Gmail agent for deterministic Gmail actions."
            )
    else:
        log(
            "Gmail agent is installed but disabled; set GMAIL_AGENT_ENABLED=true to enable the Gmail agent service."
        )
    google_docs_env = read_google_docs_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_google_docs_agent = google_docs_agent_is_configured(google_docs_env)
    if enable_google_docs_agent:
        if meaningful_env_value(google_docs_env.get("GOOGLE_DOCS_AGENT_INTERNAL_LLM_API_KEY")) is not None:
            log(
                "Google Docs agent env is configured with internal LLM credentials; bootstrap will enable and start the Google Docs agent service."
            )
        else:
            log(
                "Google Docs agent env is enabled without an internal LLM key; bootstrap will still start deterministic Docs actions, but natural-language planning will be unavailable."
            )
    else:
        log(
            "Google Docs agent is installed but disabled; set GOOGLE_DOCS_AGENT_ENABLED=true to enable it."
        )
    google_sheets_env = read_google_sheets_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_google_sheets_agent = google_sheets_agent_is_configured(google_sheets_env)
    if enable_google_sheets_agent:
        if meaningful_env_value(google_sheets_env.get("GOOGLE_SHEETS_AGENT_INTERNAL_LLM_API_KEY")) is not None:
            log(
                "Google Sheets agent env is configured with internal LLM credentials; bootstrap will enable and start the Google Sheets agent service."
            )
        else:
            log(
                "Google Sheets agent env is enabled without an internal LLM key; bootstrap will still start deterministic Sheets actions, but natural-language planning will be unavailable."
            )
    else:
        log(
            "Google Sheets agent is installed but disabled; set GOOGLE_SHEETS_AGENT_ENABLED=true to enable it."
        )
    image_env = read_image_generator_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_image_generator_agent = image_generator_agent_is_configured(image_env)
    if enable_image_generator_agent:
        log(
            "Image generator agent env is configured; bootstrap will enable and start the image generator agent service."
        )
    else:
        log(
            "Image generator agent env is not configured; bootstrap will install the unit but skip enabling the image generator agent service."
        )
    diagram_env = read_diagram_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_diagram_agent = diagram_agent_is_configured(diagram_env)
    if enable_diagram_agent:
        log(
            "Diagram agent env is configured; bootstrap will enable and start the diagram agent service."
        )
    else:
        log(
            "Diagram agent env is not configured; bootstrap will install the unit but skip enabling the diagram agent service."
        )
    map_env = read_map_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_map_agent = map_agent_is_configured(map_env)
    if enable_map_agent:
        log(
            "Map agent env is configured; bootstrap will enable and start the map agent service."
        )
    else:
        log(
            "Map agent env is not configured; bootstrap will install the unit but skip enabling the map agent service."
        )
    slide_env = read_slide_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_slide_agent = slide_agent_is_configured(slide_env)
    if enable_slide_agent:
        log(
            "Slide agent env is configured; bootstrap will enable and start the slide agent service."
        )
    else:
        log(
            "Slide agent env is not configured; bootstrap will install the unit but skip enabling the slide agent service."
        )
    alpha_env = read_alpha_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
    enable_alpha_agent = alpha_agent_is_configured(alpha_env)
    if enable_alpha_agent:
        log(
            "Alpha agent env has ALPHA_AGENT_ENABLED=true; bootstrap will enable and start the alpha agent service."
        )
    else:
        log(
            "Alpha agent is installed but disabled; set ALPHA_AGENT_ENABLED=true to enable the alpha agent service."
        )
    installed = install_systemd_units(
        systemd_template_dir,
        enable_units=enable_units,
        start_units=start_units,
        include_optional_templates=["cosmic-memory.service.example"]
        if enable_memory
        else [],
        extra_enable_units=(
            (["cosmic-memory.service"] if enable_units and enable_memory else [])
            + (
                [FIRECRAWL_AGENT_SERVICE_NAME]
                if enable_units and enable_firecrawl_agent
                else []
            )
            + (
                [X_TWITTER_SEARCH_AGENT_SERVICE_NAME]
                if enable_units and enable_x_twitter_search_agent
                else []
            )
            + (
                [TABULAR_AGENT_SERVICE_NAME]
                if enable_units and enable_tabular_agent
                else []
            )
            + (
                [EMAIL_AGENT_SERVICE_NAME]
                if enable_units and enable_email_agent
                else []
            )
            + (
                [GMAIL_AGENT_SERVICE_NAME]
                if enable_units and enable_gmail_agent
                else []
            )
            + (
                [GOOGLE_DOCS_AGENT_SERVICE_NAME]
                if enable_units and enable_google_docs_agent
                else []
            )
            + (
                [GOOGLE_SHEETS_AGENT_SERVICE_NAME]
                if enable_units and enable_google_sheets_agent
                else []
            )
            + (
                [IMAGE_GENERATOR_AGENT_SERVICE_NAME]
                if enable_units and enable_image_generator_agent
                else []
            )
            + (
                [DIAGRAM_AGENT_SERVICE_NAME]
                if enable_units and enable_diagram_agent
                else []
            )
            + (
                [MAP_AGENT_SERVICE_NAME]
                if enable_units and enable_map_agent
                else []
            )
            + (
                [SLIDE_AGENT_SERVICE_NAME]
                if enable_units and enable_slide_agent
                else []
            )
            + (
                [ALPHA_AGENT_SERVICE_NAME]
                if enable_units and enable_alpha_agent
                else []
            )
        ),
        include_memory_env=enable_memory,
    )
    if enable_units and start_units:
        run_post_provision_health_checks(
            include_memory=enable_memory,
            include_firecrawl_agent=enable_firecrawl_agent,
            include_x_twitter_search_agent=enable_x_twitter_search_agent,
            include_tabular_agent=enable_tabular_agent,
            include_email_agent=enable_email_agent,
            include_gmail_agent=enable_gmail_agent,
            include_google_docs_agent=enable_google_docs_agent,
            include_google_sheets_agent=enable_google_sheets_agent,
            include_image_generator_agent=enable_image_generator_agent,
            include_diagram_agent=enable_diagram_agent,
            include_map_agent=enable_map_agent,
            include_slide_agent=enable_slide_agent,
            include_alpha_agent=enable_alpha_agent,
        )

    print("")
    print("VM provisioning complete")
    for unit_name in installed:
        print("  unit   : {0}".format(unit_name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap COSMIC Backend on a Linux VM."
    )
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
        default=str(DEFAULT_MEMORY_REPO_DIR),
        help="cosmic-memory checkout path. Existing repos are reused; missing paths are cloned from --memory-repo-url. Default: %(default)s",
    )
    parser.add_argument(
        "--memory-repo-url",
        default=DEFAULT_MEMORY_REPO_URL,
        help="Public cosmic-memory Git URL used when --memory-repo-dir does not exist. Default: %(default)s",
    )
    parser.add_argument(
        "--memory-repo-ref",
        default=DEFAULT_MEMORY_REPO_REF,
        help="Branch or tag to clone for cosmic-memory when bootstrap fetches it. Default: %(default)s",
    )
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        help="Skip cosmic-memory checkout/install, memory.env materialization, and Neo4j provisioning.",
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
    subparsers.add_parser(
        "doctor",
        help="Check current bootstrap prerequisites without changing the system.",
    )
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
        "setup-codex-cli",
        help="Install the OpenAI Codex CLI used by the Alpha agent runner.",
    )
    subparsers.add_parser(
        "setup-cursor-cli",
        help="Install the Cursor CLI used by the Alpha agent runner.",
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
        help="Create env files, install Python and bridge deps, provision /etc/cosmic envs, install systemd units, enable them, start the backend target, and wait for core readiness.",
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
        resolve_memory_repo_dir(
            Path(getattr(args, "memory_repo_dir", "")),
            allow_missing=True,
        )
        if not bool(getattr(args, "skip_memory", False))
        and meaningful_env_value(getattr(args, "memory_repo_dir", "")) is not None
        else None
    )
    memory_repo_url = (
        meaningful_env_value(getattr(args, "memory_repo_url", ""))
        or DEFAULT_MEMORY_REPO_URL
    )
    memory_repo_ref = (
        meaningful_env_value(getattr(args, "memory_repo_ref", ""))
        or DEFAULT_MEMORY_REPO_REF
    )
    bootstrap_token = meaningful_env_value(
        getattr(args, "bootstrap_token", "")
    ) or meaningful_env_value(os.getenv("COSMIC_BOOTSTRAP_TOKEN"))
    supabase_url = (
        meaningful_env_value(getattr(args, "supabase_url", "")) or DEFAULT_SUPABASE_URL
    )
    supabase_anon_key = (
        meaningful_env_value(getattr(args, "supabase_anon_key", ""))
        or meaningful_env_value(os.getenv("COSMIC_SUPABASE_ANON_KEY"))
        or DEFAULT_SUPABASE_ANON_KEY
    )
    env_search_roots = [
        Path(item).expanduser().resolve() for item in (args.env_search_root or [])
    ] or list(DEFAULT_ENV_SEARCH_ROOTS)

    try:
        if command == "doctor":
            doctor(
                venv_path,
                requirements_path,
                bridge_dir,
                systemd_template_dir,
                env_search_roots,
            )
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
                raise BootstrapError(
                    "fetch-bootstrap-env requires --bootstrap-token or COSMIC_BOOTSTRAP_TOKEN."
                )
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
                setup_cosmic_memory(
                    venv_path,
                    memory_repo_dir,
                    memory_repo_url=memory_repo_url,
                    memory_repo_ref=memory_repo_ref,
                )
        elif command == "setup-whatsapp-bridge":
            setup_whatsapp_bridge(bridge_dir)
        elif command == "setup-codex-cli":
            ensure_openai_codex_cli()
        elif command == "setup-cursor-cli":
            ensure_cursor_cli()
        elif command == "setup-edge":
            setup_vm_edge(
                edge_setup_script,
                gateway_env_path,
                gateway_host=gateway_host,
                force=bool(getattr(args, "force_edge", False)),
                skip_if_unconfigured=False,
            )
        elif command == "install-systemd":
            if memory_repo_dir is not None:
                install_service_env_files(DEFAULT_SYSTEM_ENV_DIR, include_memory=True)
                setup_neo4j(DEFAULT_SYSTEM_ENV_DIR / "memory.env")
            else:
                install_service_env_files(DEFAULT_SYSTEM_ENV_DIR, include_memory=False)
            firecrawl_env = read_firecrawl_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_firecrawl_agent = firecrawl_agent_is_configured(firecrawl_env)
            x_twitter_env = read_x_twitter_search_agent_system_env(
                DEFAULT_SYSTEM_ENV_DIR
            )
            enable_x_twitter_search_agent = x_twitter_search_agent_is_configured(
                x_twitter_env
            )
            enable_tabular_agent = True
            email_env = read_email_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_email_agent = email_agent_enabled_via_env_or_integration(email_env)
            gmail_env = read_gmail_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_gmail_agent = gmail_agent_is_configured(gmail_env)
            google_docs_env = read_google_docs_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_google_docs_agent = google_docs_agent_is_configured(
                google_docs_env
            )
            google_sheets_env = read_google_sheets_agent_system_env(
                DEFAULT_SYSTEM_ENV_DIR
            )
            enable_google_sheets_agent = google_sheets_agent_is_configured(
                google_sheets_env
            )
            image_env = read_image_generator_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_image_generator_agent = image_generator_agent_is_configured(
                image_env
            )
            diagram_env = read_diagram_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_diagram_agent = diagram_agent_is_configured(diagram_env)
            map_env = read_map_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_map_agent = map_agent_is_configured(map_env)
            slide_env = read_slide_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_slide_agent = slide_agent_is_configured(slide_env)
            alpha_env = read_alpha_agent_system_env(DEFAULT_SYSTEM_ENV_DIR)
            enable_alpha_agent = alpha_agent_is_configured(alpha_env)
            installed = install_systemd_units(
                systemd_template_dir,
                enable_units=bool(getattr(args, "enable", False)),
                start_units=bool(getattr(args, "start", False)),
                include_optional_templates=["cosmic-memory.service.example"]
                if memory_repo_dir is not None
                else [],
                extra_enable_units=(
                    (
                        ["cosmic-memory.service"]
                        if memory_repo_dir is not None
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [FIRECRAWL_AGENT_SERVICE_NAME]
                        if enable_firecrawl_agent
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [X_TWITTER_SEARCH_AGENT_SERVICE_NAME]
                        if enable_x_twitter_search_agent
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [TABULAR_AGENT_SERVICE_NAME]
                        if enable_tabular_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [EMAIL_AGENT_SERVICE_NAME]
                        if enable_email_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [GMAIL_AGENT_SERVICE_NAME]
                        if enable_gmail_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [GOOGLE_DOCS_AGENT_SERVICE_NAME]
                        if enable_google_docs_agent
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [GOOGLE_SHEETS_AGENT_SERVICE_NAME]
                        if enable_google_sheets_agent
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [IMAGE_GENERATOR_AGENT_SERVICE_NAME]
                        if enable_image_generator_agent
                        and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [DIAGRAM_AGENT_SERVICE_NAME]
                        if enable_diagram_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [MAP_AGENT_SERVICE_NAME]
                        if enable_map_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [SLIDE_AGENT_SERVICE_NAME]
                        if enable_slide_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                    + (
                        [ALPHA_AGENT_SERVICE_NAME]
                        if enable_alpha_agent and bool(getattr(args, "enable", False))
                        else []
                    )
                ),
                include_memory_env=memory_repo_dir is not None,
            )
            if bool(getattr(args, "enable", False)) and bool(
                getattr(args, "start", False)
            ):
                run_post_provision_health_checks(
                    include_memory=memory_repo_dir is not None,
                    include_firecrawl_agent=enable_firecrawl_agent,
                    include_x_twitter_search_agent=enable_x_twitter_search_agent,
                    include_tabular_agent=enable_tabular_agent,
                    include_email_agent=enable_email_agent,
                    include_gmail_agent=enable_gmail_agent,
                    include_google_docs_agent=enable_google_docs_agent,
                    include_google_sheets_agent=enable_google_sheets_agent,
                    include_image_generator_agent=enable_image_generator_agent,
                    include_diagram_agent=enable_diagram_agent,
                    include_map_agent=enable_map_agent,
                    include_slide_agent=enable_slide_agent,
                    include_alpha_agent=enable_alpha_agent,
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
                memory_repo_url=memory_repo_url,
                memory_repo_ref=memory_repo_ref,
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
                memory_repo_url=memory_repo_url,
                memory_repo_ref=memory_repo_ref,
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
