"""Locating the Cursor CLI, and the environment it runs under.

`cursor-agent` installs itself into `$HOME/.local/bin` and self-updates in
place. Every COSMIC process that runs it overrides `HOME` to the Alpha Cursor
home (`/var/lib/cosmic/alpha/homes/cursor`), so that -- not the service user's
home -- is where the binary actually lands.

Resolution used to ignore that: it searched the *service user's* home while the
process ran under a different one. On a box where the CLI had only ever been
installed through COSMIC, the binary was right there under the Cursor home and
nothing could find it. The gateway then reported `cursor_cli_missing`, the
desktop offered a Login button that could not start anything, and the Alpha
Cursor harness was unavailable -- all while `cursor-agent status` on that same
box said the account was signed in.

Both live callers now resolve and build the environment through here, so the
binary a process runs and the `HOME` it runs it under can no longer disagree.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

CURSOR_AGENT_NAMES = (
    ("cursor-agent.exe", "cursor-agent.cmd", "cursor-agent")
    if os.name == "nt"
    else ("cursor-agent",)
)

# Kept for boxes provisioned before the CLI was installed under the Cursor home.
FALLBACK_BIN_DIRS = (
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/home/ubuntu/.local/bin"),
)


def cursor_local_bin(cursor_home: str | Path) -> Path:
    """Where `cursor-agent` installs itself when run with this HOME."""
    return Path(cursor_home).expanduser() / ".local" / "bin"


def cursor_agent_candidates(cursor_home: str | Path | None = None) -> list[Path]:
    """Every path worth probing, most authoritative first.

    The Cursor home leads: that install is the one the CLI keeps up to date
    under the HOME these processes actually set.
    """
    bin_dirs: list[Path] = []
    if cursor_home is not None:
        bin_dirs.append(cursor_local_bin(cursor_home))
    try:
        bin_dirs.append(Path.home() / ".local" / "bin")
    except (RuntimeError, OSError):
        # Path.home() needs a resolvable home; a service without one is fine.
        pass
    bin_dirs.extend(FALLBACK_BIN_DIRS)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for bin_dir in bin_dirs:
        for name in CURSOR_AGENT_NAMES:
            candidate = bin_dir / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def find_cursor_agent_binary(cursor_home: str | Path | None = None) -> str | None:
    """Absolute path to `cursor-agent`, or None when it is genuinely absent.

    `PATH` still wins when it resolves, so a box that works today keeps running
    exactly the binary it runs today; the Cursor home is an addition to the
    explicit candidates, not a change of precedence.
    """
    on_path = shutil.which("cursor-agent")
    if on_path:
        return on_path
    for candidate in cursor_agent_candidates(cursor_home):
        if candidate.exists():
            return str(candidate)
    return None


def cursor_cli_env(
    cursor_home: str | Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for running `cursor-agent` against `cursor_home`.

    `PATH` leads with the Cursor home's own `bin`, so a CLI that re-execs or
    self-updates resolves the same install this process picked.
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["HOME"] = str(cursor_home)
    env["CURSOR_AGENT"] = "1"

    path_parts = [str(cursor_local_bin(cursor_home))]
    try:
        path_parts.append(str(Path.home() / ".local" / "bin"))
    except (RuntimeError, OSError):
        pass
    path_parts.append("/usr/local/bin")
    existing = env.get("PATH", "")
    if existing:
        path_parts.append(existing)
    env["PATH"] = os.pathsep.join(path_parts)
    return apply_git_credentials(env)


def apply_git_credentials(env: dict[str, str]) -> dict[str, str]:
    """Teach git in this environment to get GitHub tokens from the Gateway.

    Injected through GIT_CONFIG_* rather than written into a gitconfig file, so
    it applies only to processes this runner starts and leaves the user's own
    git configuration completely untouched.

    GIT_TERMINAL_PROMPT=0 matters as much as the helper: without it, a headless
    agent whose credentials fail does not error - it blocks forever waiting for
    a password nobody will type.
    """
    helper_script = (
        Path(__file__).resolve().parents[1]
        / "agents"
        / "alpha_agent"
        / "git_credentials.py"
    )
    if not helper_script.exists():
        return env

    env["GIT_TERMINAL_PROMPT"] = "0"
    # Append to any GIT_CONFIG_* entries already present instead of assuming
    # index 0 is free; clobbering an existing entry would silently drop it.
    try:
        start = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        start = 0
    env[f"GIT_CONFIG_KEY_{start}"] = "credential.helper"
    env[f"GIT_CONFIG_VALUE_{start}"] = f'!"{sys.executable}" "{helper_script}"'
    env["GIT_CONFIG_COUNT"] = str(start + 1)
    return env
