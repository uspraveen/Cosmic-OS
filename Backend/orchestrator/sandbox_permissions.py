from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


_BLOCKED_HOST_PATH_PREFIXES = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/var/run",
)
_MAX_HOST_PATHS = 4
_MAX_ALLOWED_HOSTS = 6


def normalize_requested_capabilities(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    network = bool(raw.get("network"))
    host_read_paths = _normalize_host_paths(raw.get("host_read_paths"))
    host_write_paths = _normalize_host_paths(raw.get("host_write_paths"))
    allowed_hosts = _normalize_allowed_hosts(raw.get("allowed_hosts"))
    return {
        "network": network,
        "host_read_paths": host_read_paths,
        "host_write_paths": host_write_paths,
        "allowed_hosts": allowed_hosts,
    }


def capabilities_require_permission(capabilities: dict[str, Any]) -> bool:
    """Only VM file writes (edits/modifications) require user approval.

    Reads and network access are granted automatically: they are non-destructive
    and the sandbox still blocks process execution and path escapes entirely.
    """
    return bool(capabilities.get("host_write_paths"))


def build_permission_summary(capabilities: dict[str, Any], *, description: str = "") -> str:
    parts: list[str] = []
    if description:
        parts.append(description)
    if capabilities.get("network"):
        hosts = capabilities.get("allowed_hosts") or []
        if hosts:
            parts.append(f"Network access to: {', '.join(hosts)}")
        else:
            parts.append("Outbound network access")
    read_paths = capabilities.get("host_read_paths") or []
    if read_paths:
        parts.append(f"Read VM paths: {', '.join(read_paths)}")
    write_paths = capabilities.get("host_write_paths") or []
    if write_paths:
        parts.append(f"Write VM paths: {', '.join(write_paths)}")
    return " · ".join(parts) if parts else "Sandbox capability request"


def build_sandbox_permission_receipt(
    *,
    permission_id: str,
    description: str,
    capabilities: dict[str, Any],
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "permission_id": permission_id,
        "status": status,
        "description": description,
        "network": bool(capabilities.get("network")),
        "host_read_paths": list(capabilities.get("host_read_paths") or []),
        "host_write_paths": list(capabilities.get("host_write_paths") or []),
        "allowed_hosts": list(capabilities.get("allowed_hosts") or []),
        "summary": build_permission_summary(capabilities, description=description),
    }


def sandbox_grants_from_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    return {
        "network": bool(capabilities.get("network")),
        "host_read_paths": list(capabilities.get("host_read_paths") or []),
        "host_write_paths": list(capabilities.get("host_write_paths") or []),
        "allowed_hosts": list(capabilities.get("allowed_hosts") or []),
    }


def normalize_grant_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def path_within_any(path: Any, bases: Any) -> bool:
    child = normalize_grant_path(path)
    if not child:
        return False
    for base in bases or []:
        normalized_base = normalize_grant_path(base)
        if not normalized_base:
            continue
        if child == normalized_base or child.startswith(normalized_base + "/"):
            return True
    return False


def union_session_grant(grants: Any) -> dict[str, Any] | None:
    """Merge multiple approved permissions into one effective session grant."""
    if not grants:
        return None
    network = False
    read_paths: list[str] = []
    write_paths: list[str] = []
    allowed_hosts: list[str] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        if grant.get("network"):
            network = True
        for path in grant.get("host_read_paths") or []:
            normalized = normalize_grant_path(path)
            if normalized and normalized not in read_paths:
                read_paths.append(normalized)
        for path in grant.get("host_write_paths") or []:
            normalized = normalize_grant_path(path)
            if normalized and normalized not in write_paths:
                write_paths.append(normalized)
        for host in grant.get("allowed_hosts") or []:
            host_text = str(host or "").strip().lower()
            if host_text and host_text not in allowed_hosts:
                allowed_hosts.append(host_text)
    return {
        "network": network,
        "host_read_paths": read_paths,
        "host_write_paths": write_paths,
        "allowed_hosts": allowed_hosts,
    }


def session_grant_covers(requested: dict[str, Any], granted: dict[str, Any]) -> bool:
    """True when an approved session grant already covers the requested caps."""
    if not isinstance(requested, dict) or not isinstance(granted, dict):
        return False
    if requested.get("network") and not granted.get("network"):
        return False
    requested_hosts = [
        str(host).strip().lower()
        for host in (requested.get("allowed_hosts") or [])
        if str(host).strip()
    ]
    granted_hosts = [
        str(host).strip().lower()
        for host in (granted.get("allowed_hosts") or [])
        if str(host).strip()
    ]
    if requested_hosts:
        if not granted_hosts:
            return False
        for host in requested_hosts:
            if host not in granted_hosts:
                return False
    readable = list(granted.get("host_read_paths") or []) + list(
        granted.get("host_write_paths") or []
    )
    for path in requested.get("host_read_paths") or []:
        if not path_within_any(path, readable):
            return False
    writable = list(granted.get("host_write_paths") or [])
    for path in requested.get("host_write_paths") or []:
        if not path_within_any(path, writable):
            return False
    return True


def _normalize_host_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        path = _normalize_host_path(str(item or "").strip())
        if not path or path in seen:
            continue
        normalized.append(path)
        seen.add(path)
        if len(normalized) >= _MAX_HOST_PATHS:
            break
    return normalized


def _normalize_host_path(value: str) -> str | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    text = resolved.as_posix()
    lower = text.lower()
    for prefix in _BLOCKED_HOST_PATH_PREFIXES:
        if lower == prefix.rstrip("/") or lower.startswith(prefix.rstrip("/") + "/"):
            return None
    return text


def _normalize_allowed_hosts(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    for item in raw:
        host = _normalize_host_label(str(item or "").strip())
        if not host or host in seen:
            continue
        hosts.append(host)
        seen.add(host)
        if len(hosts) >= _MAX_ALLOWED_HOSTS:
            break
    return hosts


def _normalize_host_label(value: str) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        value = parsed.netloc or ""
    value = value.split("/", 1)[0].strip().lower()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if ":" in value:
        value = value.rsplit(":", 1)[0]
    if not value or not re.fullmatch(r"[a-z0-9._-]+", value):
        return None
    return value
