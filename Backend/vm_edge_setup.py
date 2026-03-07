#!/usr/bin/env python3
"""
Provision the public TLS edge for a COSMIC Gateway VM.

This script installs Caddy using the official package path for the current
Linux distro, writes a managed Caddyfile that reverse-proxies the Gateway on
127.0.0.1:8080, validates the config, and enables/restarts the Caddy service.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence
from urllib.parse import urlparse


BACKEND_ROOT = Path(__file__).resolve().parent
DEFAULT_GATEWAY_ENV_PATH = BACKEND_ROOT / "gateway.env"
DEFAULT_CADDYFILE_PATH = Path("/etc/caddy/Caddyfile")
DEFAULT_GATEWAY_UPSTREAM = "127.0.0.1:8080"
MANAGED_HEADER = "# Managed by COSMIC vm_edge_setup.py"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")
PLACEHOLDER_PATTERN = re.compile(r"^<.+>$")


class EdgeSetupError(RuntimeError):
    pass


def log(message: str) -> None:
    print("[vm-edge] {0}".format(message))


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
) -> subprocess.CompletedProcess:
    full_command = list(command)
    if use_sudo and not is_root():
        sudo_path = shutil.which("sudo")
        if not sudo_path:
            raise EdgeSetupError("Missing sudo for command: {0}".format(" ".join(command)))
        full_command = [sudo_path] + full_command

    log("Running: {0}".format(" ".join(full_command)))
    return subprocess.run(
        full_command,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def detect_package_manager() -> Optional[str]:
    for manager in ("apt-get", "dnf", "yum"):
        if shutil.which(manager):
            return manager
    return None


def parse_env_text(raw: str) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def meaningful_env_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if PLACEHOLDER_PATTERN.fullmatch(normalized):
        return None
    return normalized


def load_gateway_env(gateway_env_path: Path) -> Dict[str, str]:
    if not gateway_env_path.exists():
        return {}
    return parse_env_text(gateway_env_path.read_text(encoding="utf-8"))


def resolve_public_host(
    *,
    gateway_env_path: Path,
    explicit_host: str | None = None,
) -> str | None:
    if explicit_host:
        return normalize_public_host(explicit_host)

    env_data = load_gateway_env(gateway_env_path)
    candidate = meaningful_env_value(env_data.get("GATEWAY_PUBLIC_HOST"))
    if candidate:
        return normalize_public_host(candidate)

    public_url = meaningful_env_value(env_data.get("GATEWAY_PUBLIC_URL"))
    if public_url:
        parsed = urlparse(public_url)
        if parsed.hostname:
            return normalize_public_host(parsed.hostname)
    return None


def normalize_public_host(value: str) -> str:
    text = value.strip()
    if not text:
        raise EdgeSetupError("Gateway public host is empty")

    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            raise EdgeSetupError("Gateway public URL is missing a hostname")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise EdgeSetupError("Gateway public URL must not include a path, query, or fragment")
        text = parsed.hostname

    text = text.strip().rstrip(".")
    if text.lower() in {"localhost", "127.0.0.1", "0.0.0.0"}:
        raise EdgeSetupError("Gateway public host must be a real public hostname, not {0}".format(text))
    if not HOST_PATTERN.fullmatch(text):
        raise EdgeSetupError("Gateway public host contains unsupported characters: {0}".format(text))
    if "." not in text:
        raise EdgeSetupError("Gateway public host should be a DNS hostname, got: {0}".format(text))
    return text


def render_caddyfile(
    *,
    public_host: str,
    upstream: str,
    email: str | None = None,
) -> str:
    lines: list[str] = [MANAGED_HEADER]
    if email:
        lines.extend(
            [
                "{",
                "    email {0}".format(email),
                "}",
                "",
            ]
        )

    lines.extend(
        [
            "{0} {{".format(public_host),
            "    encode zstd gzip",
            "    reverse_proxy {0} {{".format(upstream),
            "        health_uri /health",
            "        stream_timeout 24h",
            "        stream_close_delay 5m",
            "    }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _download_text(url: str) -> str:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed vendor URL
        return response.read().decode("utf-8")


def _download_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed vendor URL
        return response.read()


def install_caddy() -> None:
    if shutil.which("caddy"):
        log("Caddy is already installed.")
        return

    if not is_linux():
        raise EdgeSetupError("Caddy edge setup currently targets Linux VMs only.")

    manager = detect_package_manager()
    if manager == "apt-get":
        _install_caddy_apt()
        return
    if manager == "dnf":
        _install_caddy_dnf()
        return

    raise EdgeSetupError(
        "Unsupported package manager for automatic Caddy install: {0}".format(manager or "missing")
    )


def _install_caddy_apt() -> None:
    packages = [
        "debian-keyring",
        "debian-archive-keyring",
        "apt-transport-https",
        "curl",
    ]
    if shutil.which("gpg") is None:
        packages.append("gnupg")

    run(["apt-get", "install", "-y", *packages], use_sudo=True)

    gpg_key = _download_bytes("https://dl.cloudsmith.io/public/caddy/stable/gpg.key")
    repo_list = _download_text("https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt")

    with tempfile.TemporaryDirectory(prefix="cosmic-caddy-apt-") as temp_dir:
        temp_path = Path(temp_dir)
        key_input_path = temp_path / "caddy.gpg.key"
        key_output_path = temp_path / "caddy-stable-archive-keyring.gpg"
        list_output_path = temp_path / "caddy-stable.list"
        key_input_path.write_bytes(gpg_key)
        list_output_path.write_text(repo_list, encoding="utf-8")

        run(["gpg", "--dearmor", "-o", str(key_output_path), str(key_input_path)])
        run(
            ["install", "-m", "644", str(key_output_path), "/usr/share/keyrings/caddy-stable-archive-keyring.gpg"],
            use_sudo=True,
        )
        run(
            ["install", "-m", "644", str(list_output_path), "/etc/apt/sources.list.d/caddy-stable.list"],
            use_sudo=True,
        )

    run(["chmod", "o+r", "/usr/share/keyrings/caddy-stable-archive-keyring.gpg"], use_sudo=True)
    run(["chmod", "o+r", "/etc/apt/sources.list.d/caddy-stable.list"], use_sudo=True)
    run(["apt-get", "update"], use_sudo=True)
    run(["apt-get", "install", "-y", "caddy"], use_sudo=True)


def _install_caddy_dnf() -> None:
    plugin_installed = False
    for package_name in ("dnf5-plugins", "dnf-plugins-core"):
        result = run(["dnf", "install", "-y", package_name], use_sudo=True, check=False)
        if result.returncode == 0:
            plugin_installed = True
            break
    if not plugin_installed:
        raise EdgeSetupError("Failed to install DNF plugin package required for Caddy COPR setup.")

    run(["dnf", "copr", "enable", "-y", "@caddy/caddy"], use_sudo=True)
    run(["dnf", "install", "-y", "caddy"], use_sudo=True)


def install_caddyfile(
    *,
    caddyfile_path: Path,
    content: str,
    force: bool = False,
) -> None:
    existing_content = None
    if caddyfile_path.exists():
        existing_content = caddyfile_path.read_text(encoding="utf-8")
        if existing_content == content:
            log("Caddyfile already up to date: {0}".format(caddyfile_path))
            return
        if MANAGED_HEADER not in existing_content and not force:
            raise EdgeSetupError(
                "Refusing to overwrite unmanaged Caddyfile at {0}. Re-run with --force if this VM is dedicated to COSMIC.".format(
                    caddyfile_path
                )
            )

    with tempfile.TemporaryDirectory(prefix="cosmic-caddyfile-") as temp_dir:
        temp_path = Path(temp_dir) / "Caddyfile"
        temp_path.write_text(content, encoding="utf-8", newline="\n")
        validate_caddyfile(temp_path)

        run(["install", "-d", "-m", "755", str(caddyfile_path.parent)], use_sudo=True)

        if existing_content is not None:
            backup_name = "{0}.bak.{1}".format(
                caddyfile_path.name,
                datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
            )
            backup_path = caddyfile_path.with_name(backup_name)
            run(["cp", str(caddyfile_path), str(backup_path)], use_sudo=True)
            log("Backed up existing Caddyfile to {0}".format(backup_path))

        run(["install", "-m", "644", str(temp_path), str(caddyfile_path)], use_sudo=True)


def validate_caddyfile(caddyfile_path: Path) -> None:
    if shutil.which("caddy") is None:
        raise EdgeSetupError("Cannot validate Caddyfile because the caddy binary is not installed.")
    run(["caddy", "validate", "--config", str(caddyfile_path), "--adapter", "caddyfile"])


def enable_and_restart_caddy() -> None:
    if shutil.which("systemctl") is None:
        raise EdgeSetupError("systemctl not found. This host does not appear to use systemd.")
    run(["systemctl", "enable", "caddy"], use_sudo=True)
    run(["systemctl", "restart", "caddy"], use_sudo=True)
    run(["systemctl", "is-active", "--quiet", "caddy"], use_sudo=True)


def doctor(gateway_env_path: Path, explicit_host: str | None = None) -> None:
    resolved_host = resolve_public_host(gateway_env_path=gateway_env_path, explicit_host=explicit_host)
    print("COSMIC VM edge doctor")
    print("  platform        : {0}".format(sys.platform))
    print("  caddy installed : {0}".format("yes" if shutil.which("caddy") else "no"))
    print("  systemctl       : {0}".format("yes" if shutil.which("systemctl") else "no"))
    print("  package manager : {0}".format(detect_package_manager() or "not found"))
    print("  gateway env     : {0}".format(gateway_env_path))
    print("  public host     : {0}".format(resolved_host or "not configured"))
    print("  caddyfile path  : {0}".format(DEFAULT_CADDYFILE_PATH))


def setup_edge(
    *,
    gateway_env_path: Path,
    explicit_host: str | None = None,
    upstream: str = DEFAULT_GATEWAY_UPSTREAM,
    caddyfile_path: Path = DEFAULT_CADDYFILE_PATH,
    email: str | None = None,
    force: bool = False,
    skip_if_unconfigured: bool = False,
) -> None:
    if not is_linux():
        raise EdgeSetupError("VM edge setup currently targets Linux VMs only.")

    public_host = resolve_public_host(gateway_env_path=gateway_env_path, explicit_host=explicit_host)
    if public_host is None:
        if skip_if_unconfigured:
            log(
                "Skipping VM edge setup because no public hostname is configured. "
                "Set GATEWAY_PUBLIC_HOST in gateway.env or pass --gateway-host."
            )
            return
        raise EdgeSetupError(
            "No public hostname configured. Set GATEWAY_PUBLIC_HOST in gateway.env or pass --gateway-host."
        )

    install_caddy()
    caddyfile = render_caddyfile(
        public_host=public_host,
        upstream=upstream,
        email=email,
    )
    install_caddyfile(caddyfile_path=caddyfile_path, content=caddyfile, force=force)
    enable_and_restart_caddy()
    print("")
    print("VM edge setup complete")
    print("  public host : {0}".format(public_host))
    print("  upstream    : {0}".format(upstream))
    print("  caddyfile   : {0}".format(caddyfile_path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Set up the public TLS edge for a COSMIC Gateway VM.")
    parser.add_argument(
        "--gateway-env",
        default=str(DEFAULT_GATEWAY_ENV_PATH),
        help="Path to gateway.env. Default: %(default)s",
    )
    parser.add_argument(
        "--gateway-host",
        default="",
        help="Public DNS hostname for the Gateway. Overrides GATEWAY_PUBLIC_HOST in gateway.env.",
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_GATEWAY_UPSTREAM,
        help="Gateway upstream address for Caddy reverse_proxy. Default: %(default)s",
    )
    parser.add_argument(
        "--caddyfile-path",
        default=str(DEFAULT_CADDYFILE_PATH),
        help="Target Caddyfile path. Default: %(default)s",
    )
    parser.add_argument(
        "--email",
        default="",
        help="Optional ACME account email to include in the global Caddy options block.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing unmanaged Caddyfile. Use only on dedicated COSMIC VMs.",
    )
    parser.add_argument(
        "--skip-if-unconfigured",
        action="store_true",
        help="Exit successfully if no public host is configured instead of failing.",
    )

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="Check the current VM edge prerequisites and configuration.")
    subparsers.add_parser("setup", help="Install/configure Caddy and the managed COSMIC Caddyfile.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "setup"
    gateway_env_path = Path(args.gateway_env).expanduser().resolve()
    caddyfile_path = Path(args.caddyfile_path).expanduser().resolve()
    gateway_host = args.gateway_host.strip() or None
    email = args.email.strip() or None

    try:
        if command == "doctor":
            doctor(gateway_env_path, explicit_host=gateway_host)
        else:
            setup_edge(
                gateway_env_path=gateway_env_path,
                explicit_host=gateway_host,
                upstream=args.upstream,
                caddyfile_path=caddyfile_path,
                email=email,
                force=bool(args.force),
                skip_if_unconfigured=bool(args.skip_if_unconfigured),
            )
    except EdgeSetupError as exc:
        print("VM edge setup failed: {0}".format(exc), file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(
            "VM edge setup failed while running: {0} (exit={1})".format(
                " ".join(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]),
                exc.returncode,
            ),
            file=sys.stderr,
        )
        return exc.returncode or 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
