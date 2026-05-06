from __future__ import annotations

import sqlite3
from pathlib import Path

from agents.alpha_agent.artifact_promoter import promote_alpha_artifacts
from agents.alpha_agent.codex_runner import CodexWorkspaceRunner
from agents.alpha_agent.config import AlphaAgentConfig
from agents.alpha_agent.cursor_runner import CursorWorkspaceRunner, normalize_cursor_model
from agents.alpha_agent.docker_runner import DockerWorkspaceRunner
from agents.alpha_agent.project_registry import ProjectRegistry
from agents.alpha_agent.workspace_manager import WorkspaceManager


def _config(tmp_path: Path) -> AlphaAgentConfig:
    return AlphaAgentConfig(
        redis_url="redis://127.0.0.1:6379/0",
        gateway_url="http://127.0.0.1:8080",
        gateway_internal_token="token",
        orchestrator_url="http://127.0.0.1:8743",
        orchestrator_internal_token="token",
        enabled=False,
        alpha_root=tmp_path / "alpha",
        project_db_path=tmp_path / "projects.db",
        docker_image="ubuntu:24.04",
        docker_network="bridge",
        docker_memory="4g",
        docker_cpus="2",
        docker_pids_limit=512,
        docker_timeout_sec=300.0,
        allow_docker_smoke=False,
        codex_home=tmp_path / "alpha" / "homes" / "codex",
        cursor_home=tmp_path / "alpha" / "homes" / "cursor",
        codex_sandbox="workspace-write",
        codex_timeout_sec=14400.0,
        codex_default_model="",
        cursor_timeout_sec=14400.0,
        cursor_default_model="",
        cursor_init_timeout_sec=180.0,
        cli_idle_check_sec=300.0,
    )


def test_project_registry_creates_and_resolves_project(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "projects.db")
    created = registry.create_project(
        aliases=["portfolio"],
        repo_url="https://example.test/repo.git",
        last_task_id="tsk_abc",
        last_session_id="sess_1",
        summary="Build a portfolio site",
    )

    assert created.project_id.startswith("prj_")
    assert registry.find_project(created.project_id).project_id == created.project_id
    assert registry.find_project("portfolio").project_id == created.project_id
    assert registry.find_project("https://example.test/repo.git").project_id == created.project_id
    assert registry.find_project("") is None

    updated = registry.mark_task(
        created.project_id,
        task_id="tsk_def",
        session_id="sess_2",
        local_path="/var/lib/cosmic/alpha/workspaces/prj_x",
        status="workspace_prepared",
    )
    assert updated.last_task_id == "tsk_def"
    assert updated.last_session_id == "sess_2"
    assert updated.status == "workspace_prepared"


def test_project_registry_scores_task_search_fields(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path / "projects.db")
    site = registry.create_project(
        aliases=["cosmic-site"],
        last_task_id="tsk_site_initial",
        last_session_id="sess_alpha",
        goal="Build and host a website for Cosmic",
        summary="Initial website deployment",
    )
    archive = registry.create_project(
        aliases=["archive-site"],
        last_task_id="tsk_archive_initial",
        last_session_id="sess_alpha",
        goal="Build an archive website for notes",
        summary="Older website project",
    )

    registry.mark_task(
        site.project_id,
        task_id="tsk_site_screenshot",
        session_id="sess_beta",
        goal="Take a screenshot of the live white background website",
        context_brief="Playwright capture for the site running on port 8000.",
        preferred_harness="cursor",
        summary="Captured a PNG screenshot of the white COSMIC landing page.",
        status="completed",
        artifact_ids=["art_site_screenshot_png"],
        deployment_url="http://3.21.236.10:8000/",
    )
    registry.mark_task(
        archive.project_id,
        task_id="tsk_archive_nav",
        session_id="sess_beta",
        goal="Update archive website navigation",
        summary="Navigation polish for another site.",
        status="completed",
    )

    candidates = registry.search_projects("screenshot white website", session_id="sess_beta", limit=3)

    assert candidates[0].project.project_id == site.project_id
    assert candidates[0].score > candidates[1].score
    assert "task_goal" in candidates[0].matched_fields
    assert "task_summary" in candidates[0].matched_fields
    assert registry.find_project("art_site_screenshot_png").project_id == site.project_id
    assert registry.find_project("http://3.21.236.10:8000/").project_id == site.project_id


def test_project_registry_migrates_legacy_project_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "projects.db"
    registry = ProjectRegistry(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE alpha_projects (
                project_id TEXT PRIMARY KEY,
                aliases TEXT NOT NULL DEFAULT '[]',
                repo_url TEXT,
                local_path TEXT,
                deployment_url TEXT,
                last_task_id TEXT,
                last_session_id TEXT,
                harness_thread_ids TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL,
                summary TEXT,
                artifact_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO alpha_projects (
                project_id, aliases, repo_url, local_path, deployment_url,
                last_task_id, last_session_id, harness_thread_ids, status,
                summary, artifact_ids, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prj_legacy",
                '["legacy-site"]',
                None,
                None,
                None,
                "tsk_legacy",
                "sess_legacy",
                "[]",
                "prepared",
                "Legacy Alpha project",
                "[]",
                "2026-05-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
            ),
        )

    updated = registry.mark_task(
        "prj_legacy",
        task_id="tsk_modern",
        session_id="sess_modern",
        goal="Modern searchable migration goal",
        summary="Migrated project can now be searched by task metadata.",
        artifact_ids=["art_modern"],
        status="completed",
    )

    assert updated.goal == "Modern searchable migration goal"
    assert registry.find_project("legacy-site").project_id == "prj_legacy"
    assert registry.find_project("art_modern").project_id == "prj_legacy"
    assert registry.search_projects("migration goal")[0].project.project_id == "prj_legacy"


def test_workspace_manager_prepares_expected_layout(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    manager = WorkspaceManager(tmp_path / "alpha", codex_home=codex_home)
    paths = manager.prepare(project_id="prj_abc123", task_id="tsk_def456")

    assert paths.workspace.is_dir()
    assert paths.artifacts.is_dir()
    assert paths.codex_home == codex_home
    assert paths.codex_home.is_dir()
    assert paths.opencode_home.is_dir()
    assert paths.cursor_home.is_dir()
    assert paths.workspace.name == "prj_abc123"
    assert paths.artifacts.name == "tsk_def456"


def test_docker_runner_builds_isolated_command_without_docker_socket(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(cfg.alpha_root).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    runner = DockerWorkspaceRunner(cfg)

    command = runner.build_command(paths=paths, command=["sh", "-lc", "pwd"])
    joined = " ".join(command)

    assert "docker.sock" not in joined
    assert "--workdir /workspace" in joined
    assert f"{paths.workspace.resolve()}:/workspace" in joined
    assert f"{paths.artifacts.resolve()}:/artifacts" in joined
    assert f"{paths.codex_home.resolve()}:/codex-home" in joined
    assert command[-4:] == ["ubuntu:24.04", "sh", "-lc", "pwd"]


def test_codex_runner_builds_workspace_write_exec_command(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(cfg.alpha_root, codex_home=cfg.codex_home).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    runner = CodexWorkspaceRunner(cfg)

    command = runner.build_command(
        paths=paths,
        prompt="Build the app",
        output_path=paths.artifacts / "codex-last-message.md",
        model="gpt-5.1-codex",
    )
    joined = " ".join(command)

    assert "exec" in command
    assert "--cd" in command
    assert str(paths.workspace) in command
    assert "--sandbox workspace-write" in joined
    assert "--skip-git-repo-check" in joined
    assert "--output-last-message" in command
    assert "gpt-5.1-codex" in command
    assert command[-1] == "-"


def test_cursor_runner_builds_headless_stream_command(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(
        cfg.alpha_root,
        codex_home=cfg.codex_home,
        cursor_home=cfg.cursor_home,
    ).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    runner = CursorWorkspaceRunner(cfg)

    command = runner.build_command(
        paths=paths,
        prompt="Build the app",
        model="gpt-5",
        stream_json=True,
    )

    assert "--print" in command
    assert "--force" in command
    assert "--trust" in command
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "disabled"
    assert "--output-format" in command
    assert "stream-json" in command
    assert "--model" in command
    assert "gpt-5" in command
    assert command[-1] == "Build the app"


def test_cursor_runner_maps_normal_composer_to_working_alias(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(
        cfg.alpha_root,
        codex_home=cfg.codex_home,
        cursor_home=cfg.cursor_home,
    ).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    runner = CursorWorkspaceRunner(cfg)

    command = runner.build_command(
        paths=paths,
        prompt="Build the app",
        model="Composer 2",
        stream_json=True,
    )

    assert "--model" in command
    model_index = command.index("--model") + 1
    assert command[model_index] == "composer-2"
    assert "composer-2-fast" not in command


def test_cursor_model_normalization_preserves_explicit_fast_variant() -> None:
    assert normalize_cursor_model("composer") == "composer-2"
    assert normalize_cursor_model("composer-2") == "composer-2"
    assert normalize_cursor_model("Composer 2") == "composer-2"
    assert normalize_cursor_model("composer-2-fast") == "composer-2-fast"
    assert normalize_cursor_model("auto") is None


def test_cursor_runner_detects_fast_model_mismatch(tmp_path: Path) -> None:
    runner = CursorWorkspaceRunner(_config(tmp_path))
    stdout = '{"type":"system","subtype":"init","model":"Composer 2 Fast"}\n'

    assert runner._extract_observed_model(stdout) == "Composer 2 Fast"
    assert not runner._model_mismatch(
        requested_model="composer-2",
        observed_model="Composer 2",
    )
    assert runner._model_mismatch(
        requested_model="composer-2",
        observed_model="Composer 2 Fast",
    )
    assert not runner._model_mismatch(
        requested_model="composer-2-fast",
        observed_model="Composer 2 Fast",
    )


async def _collect_stream_lines(payload: bytes) -> list[str]:
    from agents.alpha_agent.streaming import iter_stream_lines

    class Reader:
        def __init__(self, data: bytes) -> None:
            self._data = bytearray(data)

        async def read(self, size: int) -> bytes:
            if not self._data:
                return b""
            chunk = self._data[:size]
            del self._data[:size]
            return bytes(chunk)

    return [line async for line in iter_stream_lines(Reader(payload), max_line_bytes=32, chunk_size=8)]


def test_cli_stream_reader_omits_oversized_jsonl_event() -> None:
    import asyncio

    lines = asyncio.run(_collect_stream_lines(b'{"type":"small"}\n' + b"x" * 80 + b"\n"))

    assert lines[0] == '{"type":"small"}\n'
    assert "cosmic.large_cli_event_omitted" in lines[1]


def test_promote_alpha_artifacts_includes_task_artifact_dir_files(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(
        cfg.alpha_root,
        codex_home=cfg.codex_home,
        cursor_home=cfg.cursor_home,
    ).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    screenshot = paths.artifacts / "site-screenshot.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    artifacts = promote_alpha_artifacts(
        task_id="tsk_def456",
        paths=paths,
        text_hints=[],
    )

    assert len(artifacts) == 1
    assert artifacts[0].mime == "image/png"
    assert artifacts[0].path == str(screenshot.resolve())


def test_promote_alpha_artifacts_includes_referenced_workspace_files(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    paths = WorkspaceManager(
        cfg.alpha_root,
        codex_home=cfg.codex_home,
        cursor_home=cfg.cursor_home,
    ).prepare(
        project_id="prj_abc123",
        task_id="tsk_def456",
    )
    bundle = paths.workspace / "export.zip"
    bundle.write_bytes(b"zip bytes")

    artifacts = promote_alpha_artifacts(
        task_id="tsk_def456",
        paths=paths,
        text_hints=[f"Saved final export at {bundle.resolve()}."],
    )

    assert len(artifacts) == 1
    assert artifacts[0].mime == "application/zip"
    assert artifacts[0].path == str(bundle.resolve())
