"""Cursor CLI resolution must agree with the HOME the CLI is run under.

Production failure this pins: `cursor-agent` was installed and signed in at
`/var/lib/cosmic/alpha/homes/cursor/.local/bin/cursor-agent`, because every
COSMIC process runs it with `HOME` set to that Cursor home and the CLI installs
itself into `$HOME/.local/bin`. Resolution searched the *service user's* home
instead, found nothing, and reported `cursor_cli_missing` - so the gateway's
Login button could not start a login and the Alpha Cursor harness was dead,
while `cursor-agent status` on the same box said the account was signed in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.cursor_cli import (  # noqa: E402
    cursor_cli_env,
    cursor_local_bin,
    find_cursor_agent_binary,
)


@pytest.fixture
def isolated_lookup(monkeypatch, tmp_path):
    """No `cursor-agent` on PATH, and a service home that does not have one."""
    monkeypatch.setattr("shared.cursor_cli.shutil.which", lambda _name: None)
    service_home = tmp_path / "service-home"
    service_home.mkdir()
    monkeypatch.setattr("shared.cursor_cli.Path.home", classmethod(lambda _cls: service_home))
    return service_home


def _install(cursor_home: Path) -> Path:
    binary = cursor_local_bin(cursor_home) / "cursor-agent"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    return binary


def test_finds_the_cli_installed_under_the_cursor_home(isolated_lookup, tmp_path):
    cursor_home = tmp_path / "alpha" / "homes" / "cursor"
    binary = _install(cursor_home)

    assert find_cursor_agent_binary(cursor_home) == str(binary)


def test_reports_missing_only_when_it_really_is(isolated_lookup, tmp_path):
    assert find_cursor_agent_binary(tmp_path / "empty-cursor-home") is None


def test_path_still_wins_so_working_boxes_keep_their_binary(monkeypatch, tmp_path):
    """Precedence is unchanged: the Cursor home is an added candidate, not a
    reordering. A box that resolves through PATH today runs the same binary."""
    monkeypatch.setattr("shared.cursor_cli.shutil.which", lambda _name: "/usr/local/bin/cursor-agent")
    cursor_home = tmp_path / "cursor-home"
    _install(cursor_home)

    assert find_cursor_agent_binary(cursor_home) == "/usr/local/bin/cursor-agent"


def test_resolution_survives_a_service_account_with_no_home(monkeypatch, tmp_path):
    def _no_home(_cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr("shared.cursor_cli.shutil.which", lambda _name: None)
    monkeypatch.setattr("shared.cursor_cli.Path.home", classmethod(_no_home))
    cursor_home = tmp_path / "cursor-home"
    binary = _install(cursor_home)

    assert find_cursor_agent_binary(cursor_home) == str(binary)


def test_env_puts_the_cursor_home_bin_first_on_path(tmp_path):
    """A CLI that re-execs or self-updates has to resolve the same install this
    process picked, not some older copy earlier on PATH."""
    cursor_home = tmp_path / "cursor-home"
    env = cursor_cli_env(cursor_home, base_env={"PATH": "/usr/bin"})

    assert env["HOME"] == str(cursor_home)
    assert env["CURSOR_AGENT"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == str(cursor_local_bin(cursor_home))
    assert "/usr/bin" in env["PATH"].split(os.pathsep)


def test_env_does_not_mutate_the_environment_it_was_given(tmp_path):
    base = {"PATH": "/usr/bin", "HOME": "/home/ubuntu"}
    cursor_cli_env(tmp_path / "cursor-home", base_env=base)

    assert base == {"PATH": "/usr/bin", "HOME": "/home/ubuntu"}


def test_the_runner_and_the_gateway_resolve_through_the_same_helper():
    """The two callers drifting apart is what produced the outage; neither may
    keep a private copy of the search order."""
    from agents.alpha_agent import cursor_runner
    from gateway import runtime

    assert cursor_runner.resolve_cursor_agent_binary is find_cursor_agent_binary
    assert runtime.find_cursor_agent_binary is find_cursor_agent_binary
    assert runtime.cursor_cli_env is cursor_cli_env
