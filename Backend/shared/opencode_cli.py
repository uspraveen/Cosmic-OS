"""Locating the OpenCode CLI, and the environment it runs under.

`opencode` reads its global rules from `~/.config/opencode/AGENTS.md`, keeps
session/auth state under `~/.local/share/opencode`, and installs its own
binary into `~/.opencode/bin` when installed via the official install script.
Every COSMIC process that runs the CLI overrides `HOME` to the Alpha OpenCode
home (`/var/lib/cosmic/alpha/homes/opencode`), so all of that state — rules,
auth, sessions, self-updated binaries — stays inside one home that the Alpha
workspace manager already creates, and can never collide with a human's
personal OpenCode installation.

The XDG variables are pinned explicitly (not left to fall out of HOME) so the
layout is identical regardless of the distro defaults.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# Single source of truth for git credential-helper injection (shared with the
# Cursor CLI env builder).
from shared.cursor_cli import apply_git_credentials

OPENCODE_NAMES = (
    ("opencode.exe", "opencode.cmd", "opencode")
    if os.name == "nt"
    else ("opencode",)
)

# Kept for boxes provisioned before the CLI was reachable from the service
# user: npm -g installs (node prefix bin) and distro-packaged locations.
FALLBACK_BIN_DIRS = (
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/home/ubuntu/.local/bin"),
)


def opencode_local_bin_dir(opencode_home: str | Path) -> Path:
    """Where the official install script puts `opencode` under this HOME."""
    return Path(opencode_home).expanduser() / ".opencode" / "bin"


def opencode_config_dir(opencode_home: str | Path) -> Path:
    """Global config dir: `<home>/.config/opencode` (rules live here too)."""
    return Path(opencode_home).expanduser() / ".config" / "opencode"


def opencode_data_dir(opencode_home: str | Path) -> Path:
    """Session/auth/snapshot dir (XDG_DATA_HOME/opencode)."""
    return Path(opencode_home).expanduser() / ".local" / "share" / "opencode"


def _user_local_bin_dir() -> Path | None:
    try:
        return Path.home() / ".local" / "bin"
    except (RuntimeError, OSError):
        return None


def opencode_candidates(opencode_home: str | Path | None = None) -> list[Path]:
    """Every path worth probing, most authoritative first."""
    bin_dirs: list[Path] = []
    if opencode_home is not None:
        bin_dirs.append(opencode_local_bin_dir(opencode_home))
    try:
        bin_dirs.append(Path.home() / ".opencode" / "bin")
    except (RuntimeError, OSError):
        pass
    user_local = _user_local_bin_dir()
    if user_local is not None:
        bin_dirs.append(user_local)
    bin_dirs.extend(FALLBACK_BIN_DIRS)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for bin_dir in bin_dirs:
        for name in OPENCODE_NAMES:
            candidate = bin_dir / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def find_opencode_binary(opencode_home: str | Path | None = None) -> str | None:
    """Absolute path to `opencode`, or None when it is genuinely absent.

    `PATH` still wins when it resolves, mirroring cursor resolution: a box
    that works today keeps running exactly the binary it runs today; the
    OpenCode home adds explicit candidates rather than changing precedence.
    """
    on_path = shutil.which("opencode")
    if on_path:
        return on_path
    for candidate in opencode_candidates(opencode_home):
        if candidate.exists():
            return str(candidate)
    return None


def build_provider_keys_config_content(provider_keys: dict[str, str]) -> str | None:
    """Inline JSON for OPENCODE_CONFIG_CONTENT authenticating multiple providers.

    Keys are keyed by provider id (`anthropic`, `openai`, `zen-opencode`,
    …). Injected per run instead of written to disk, so credentials never
    land in a file the workspace CLIs could read back into context. Returns
    None when there is nothing to inject.
    """
    cleaned = {
        str(pid).strip().lower(): str(key).strip()
        for pid, key in (provider_keys or {}).items()
        if str(pid or "").strip() and str(key or "").strip()
    }
    if not cleaned:
        return None
    return json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                pid: {"options": {"apiKey": key}} for pid, key in sorted(cleaned.items())
            },
        }
    )


def build_zen_api_key_config_content(api_key: str) -> str | None:
    """Single-provider convenience wrapper (Zen id is `opencode`)."""
    return build_provider_keys_config_content({"opencode": api_key})


def opencode_cli_env(
    opencode_home: str | Path,
    *,
    provider_keys: dict[str, str] | None = None,
    zen_api_key: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for running `opencode` against the Alpha OpenCode home.

    PATH leads with the home's own install dir so a re-exec/self-upgrade
    resolves the same install this process picked. COSMIC manages upgrades
    itself (gateway update loop + bootstrap), so the CLI's interactive
    auto-updater is disabled — a silent mid-task binary swap would be
    indistinguishable from a harness crash.

    `provider_keys` wins over the deprecated single `zen_api_key`.
    """
    env = dict(base_env if base_env is not None else os.environ)
    home = Path(opencode_home).expanduser()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["OPENCODE_CONFIG_DIR"] = str(opencode_config_dir(home))
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"

    path_parts = [str(opencode_local_bin_dir(home))]
    try:
        path_parts.append(str(Path.home() / ".local" / "bin"))
    except (RuntimeError, OSError):
        pass
    path_parts.append("/usr/local/bin")
    existing = env.get("PATH", "")
    if existing:
        path_parts.append(existing)
    env["PATH"] = os.pathsep.join(path_parts)

    keys = dict(provider_keys or {})
    if not keys and (zen_api_key or "").strip():
        keys = {"opencode": zen_api_key.strip()}
    content = build_provider_keys_config_content(keys)
    # Never leak through to child processes we didn't inject on purpose.
    if content:
        env.setdefault("OPENCODE_CONFIG_CONTENT", content)

    return apply_git_credentials(env)
