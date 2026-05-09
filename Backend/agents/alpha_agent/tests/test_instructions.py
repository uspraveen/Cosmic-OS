"""Tests for Alpha agent instruction renderers and writers."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from agents.alpha_agent import instructions
from agents.alpha_agent.instructions import (
    RuntimeMode,
    VmFacts,
    detect_runtime_mode,
    ensure_alpha_global_instructions,
    ensure_codex_global_instructions,
    ensure_cursor_global_instructions,
    render_global_instructions,
    render_workspace_instructions,
    seed_workspace_instructions,
)


_FACTS = VmFacts(
    hostname="vm-test",
    primary_ip="172.31.0.5",
    kernel="6.1.0-test",
    os_release="Ubuntu 24.04 LTS",
    public_ip="3.12.241.200",
    public_hostname="vm-test.thelearnchain.com",
)


_FACTS_NO_PUBLIC = VmFacts(
    hostname="vm-test",
    primary_ip="172.31.0.5",
    kernel="6.1.0-test",
    os_release="Ubuntu 24.04 LTS",
    public_ip="",
    public_hostname="",
)


def _global_codex(**overrides) -> str:
    kwargs = dict(
        cli="codex",
        runtime_mode=RuntimeMode.HOST,
        vm_facts=_FACTS,
        capabilities=[("bash", "shell"), ("yt-dlp", "yt download")],
    )
    kwargs.update(overrides)
    return render_global_instructions(**kwargs)


# ── Renderer: structure & required content ───────────────────────────────────


def test_global_instructions_has_eight_numbered_sections():
    text = _global_codex()
    for n in range(1, 9):
        assert f"## {n} ·" in text, f"missing section {n} in global instructions"


def test_global_instructions_includes_persona_and_voice_anchors():
    text = _global_codex()
    # Persona — Alpha as the break-glass operator
    assert "break-glass operator" in text
    assert "specialist" in text
    # Voice — terse, operator-grade
    assert "Terse" in text or "terse" in text
    assert "Operator-grade" in text or "operator-grade" in text


def test_global_instructions_includes_localhost_eq_production_warning():
    """The headline bug we are fixing: SSH to localhost when path is local."""
    text = _global_codex()
    assert "localhost" in text.lower()
    assert "production" in text.lower()
    assert "SSH" in text or "ssh" in text


def test_runtime_block_surfaces_public_ip_and_hostname_when_available():
    text = _global_codex()
    assert "PRIVATE IP:" in text
    assert "172.31.0.5" in text
    assert "PUBLIC IP:" in text
    assert "3.12.241.200" in text
    assert "PUBLIC HOSTNAME:" in text
    assert "vm-test.thelearnchain.com" in text


def test_runtime_block_omits_public_lines_when_unavailable():
    text = _global_codex(vm_facts=_FACTS_NO_PUBLIC)
    assert "PRIVATE IP:" in text
    assert "PUBLIC IP:" not in text
    assert "PUBLIC HOSTNAME:" not in text


def test_runtime_block_tells_alpha_to_prefer_public_hostname_for_user_links():
    """Alpha should share the stable FQDN, not the auto-rotated public IP."""
    text = _global_codex()
    runtime_block_start = text.find("PUBLIC HOSTNAME:")
    runtime_block_end = text.find("OS:", runtime_block_start)
    fragment = text[runtime_block_start:runtime_block_end].lower()
    assert "user" in fragment and "fqdn" in fragment


def test_global_instructions_lists_detected_capabilities():
    text = _global_codex(
        capabilities=[
            ("bash", "shell"),
            ("yt-dlp", "youtube"),
            ("ffmpeg", "media"),
        ]
    )
    assert "yt-dlp" in text
    assert "ffmpeg" in text
    assert "bash" in text


def test_codex_and_cursor_variants_have_distinct_identity_lines():
    codex = _global_codex(cli="codex")
    cursor = render_global_instructions(
        cli="cursor",
        runtime_mode=RuntimeMode.HOST,
        vm_facts=_FACTS,
        capabilities=[("bash", "shell")],
    )
    assert "Codex CLI" in codex
    assert "Cursor CLI" in cursor
    assert "Codex CLI" not in cursor
    assert "Cursor CLI" not in codex


def test_runtime_block_reflects_docker_when_in_container():
    text = _global_codex(runtime_mode=RuntimeMode.DOCKER)
    assert "Docker" in text


def test_runtime_block_reflects_codex_workspace_write_sandbox():
    text = _global_codex(runtime_mode=RuntimeMode.CODEX_SANDBOX_WORKSPACE_WRITE)
    assert "workspace-write" in text
    assert "Network egress is restricted" in text


def test_runtime_block_reflects_codex_read_only_sandbox():
    text = _global_codex(runtime_mode=RuntimeMode.CODEX_SANDBOX_READ_ONLY)
    assert "read-only" in text
    assert "CANNOT write" in text


def test_global_instructions_does_not_leak_secrets():
    """Sanity: nothing in the renderer should pull env vars or write tokens."""
    text = _global_codex()
    for needle in ("API_KEY", "TOKEN", "SECRET", "Bearer ", "sk-"):
        assert needle not in text, f"unexpected secret-shaped string: {needle!r}"


def test_global_instructions_size_is_reasonable():
    """Size budget: < 7 KB so we don't burn CLI prompt context."""
    text = _global_codex()
    assert len(text.encode("utf-8")) < 7000, f"too big: {len(text)} bytes"


# ── Runtime detection ────────────────────────────────────────────────────────


def test_detect_runtime_mode_codex_sandbox_dominates_for_codex():
    mode = detect_runtime_mode(cli="codex", codex_sandbox="workspace-write")
    assert mode is RuntimeMode.CODEX_SANDBOX_WORKSPACE_WRITE


def test_detect_runtime_mode_codex_full_access_falls_through_to_host_or_docker():
    """`danger-full-access` is host-equivalent, so detection should NOT lock
    into the codex-sandbox mode and should report the underlying environment."""
    mode = detect_runtime_mode(cli="codex", codex_sandbox="danger-full-access")
    assert mode in (RuntimeMode.HOST, RuntimeMode.DOCKER)


def test_detect_runtime_mode_cursor_ignores_sandbox_arg():
    mode = detect_runtime_mode(cli="cursor", codex_sandbox="read-only")
    assert mode in (RuntimeMode.HOST, RuntimeMode.DOCKER)


# ── Per-workspace overlay ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FakeProject:
    project_id: str = "prj_portfolio_site"
    aliases: list[str] = None  # type: ignore[assignment]
    repo_url: str | None = None
    deployment_url: str | None = None
    last_task_id: str | None = None
    summary: str | None = None
    goal: str | None = None
    context_brief: str | None = None
    preferred_harness: str | None = None


def test_workspace_overlay_renders_durable_user_facts():
    project = _FakeProject(
        project_id="prj_portfolio",
        aliases=["portfolio_site"],
        repo_url="https://github.com/u/portfolio.git",
        deployment_url="http://localhost/",
        last_task_id="tsk_abc123",
        summary="Built blog post and fixed likes API",
        goal="Add a new post and fix the likes counter regression",
        context_brief="The site is deployed on this same VM at /tmp/site/. "
                      "Deploy means rsync into /tmp/site, never SSH out.",
        preferred_harness="codex",
    )
    text = render_workspace_instructions(
        project=project,
        workspace_path=Path("/var/lib/cosmic/alpha/workspaces/prj_portfolio"),
        artifacts_path=Path("/var/lib/cosmic/alpha/artifacts/tsk_abc123"),
    )
    assert "prj_portfolio" in text
    assert "/tmp/site" in text  # the durable user fact
    assert "tsk_abc123" in text
    assert "codex" in text
    assert "Stated goal" in text
    assert "Durable context" in text


def test_workspace_overlay_handles_empty_project_record():
    text = render_workspace_instructions(
        project=None,
        workspace_path=Path("/tmp/wp"),
    )
    assert "(new)" in text or "No prior context" in text
    assert "Workspace path" in text


def test_workspace_overlay_truncates_runaway_fields():
    big = "x" * 5000
    project = _FakeProject(goal=big, context_brief=big, summary=big)
    text = render_workspace_instructions(
        project=project, workspace_path=Path("/tmp/wp")
    )
    # Each field is truncated; total stays well under 5x the longest budget.
    assert len(text) < 4000


# ── Idempotent writers ──────────────────────────────────────────────────────


def test_ensure_codex_global_instructions_creates_file(tmp_path):
    home = tmp_path / "codex"
    result = ensure_codex_global_instructions(codex_home=home)
    written = home / instructions.CODEX_GLOBAL_INSTRUCTIONS_RELATIVE
    assert written.exists()
    assert "Alpha — COSMIC Operator" in written.read_text(encoding="utf-8")
    assert result["wrote"] is True


def test_ensure_codex_global_instructions_is_idempotent(tmp_path):
    home = tmp_path / "codex"
    first = ensure_codex_global_instructions(codex_home=home)
    second = ensure_codex_global_instructions(codex_home=home)
    assert first["wrote"] is True
    assert second["wrote"] is False  # same content => no rewrite


def test_ensure_codex_global_instructions_rewrites_on_change(tmp_path):
    home = tmp_path / "codex"
    target = home / instructions.CODEX_GLOBAL_INSTRUCTIONS_RELATIVE
    ensure_codex_global_instructions(codex_home=home)
    target.write_text("user-corrupted contents\n", encoding="utf-8")
    result = ensure_codex_global_instructions(codex_home=home)
    assert result["wrote"] is True
    assert "Alpha — COSMIC Operator" in target.read_text(encoding="utf-8")


def test_ensure_cursor_global_instructions_writes_into_cursor_rules_path(tmp_path):
    home = tmp_path / "cursor"
    result = ensure_cursor_global_instructions(cursor_home=home)
    expected = home / ".cursor" / "rules" / "cosmic.md"
    assert expected.exists()
    assert result["path"] == str(expected)
    assert "Cursor CLI" in expected.read_text(encoding="utf-8")


def test_ensure_alpha_global_instructions_writes_both_when_both_passed(tmp_path):
    codex = tmp_path / "codex"
    cursor = tmp_path / "cursor"
    result = ensure_alpha_global_instructions(
        codex_home=codex, cursor_home=cursor
    )
    assert "codex" in result and "cursor" in result
    assert (codex / "AGENTS.md").exists()
    assert (cursor / ".cursor" / "rules" / "cosmic.md").exists()


def test_ensure_alpha_global_instructions_handles_partial_args(tmp_path):
    codex = tmp_path / "codex"
    result = ensure_alpha_global_instructions(codex_home=codex)
    assert "codex" in result
    assert "cursor" not in result


def test_seed_workspace_instructions_writes_file(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = _FakeProject(
        project_id="prj_x",
        goal="test",
        context_brief="The site is on this VM at /tmp/site",
    )
    result = seed_workspace_instructions(
        workspace_path=workspace,
        artifacts_path=tmp_path / "art",
        project=project,
    )
    target = workspace / "AGENTS.md"
    assert target.exists()
    assert result["wrote"] is True
    text = target.read_text(encoding="utf-8")
    assert "/tmp/site" in text
    assert "prj_x" in text


def test_seed_workspace_instructions_is_idempotent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    project = _FakeProject(project_id="prj_x", goal="test goal")
    a = seed_workspace_instructions(workspace_path=workspace, project=project)
    b = seed_workspace_instructions(workspace_path=workspace, project=project)
    assert a["wrote"] is True
    assert b["wrote"] is False


def test_writer_failure_returns_error_marker_without_raising(
    tmp_path, monkeypatch, caplog
):
    """An I/O failure must be logged and surfaced as `error: True`, never raised.

    Callers (runners, gateway login handlers) wrap these in best-effort blocks
    but the writers themselves should also fail soft so a transient FS issue
    doesn't break a CLI run.
    """

    def _explode(self, *_a, **_kw):
        raise OSError("synthetic I/O failure")

    monkeypatch.setattr(Path, "write_text", _explode)
    with caplog.at_level(logging.ERROR, logger="agents.alpha_agent.instructions"):
        result = ensure_codex_global_instructions(codex_home=tmp_path / "codex")
    assert result.get("error") is True
    assert any(
        "alpha.instructions.codex_write_failed" in record.message
        for record in caplog.records
    )
