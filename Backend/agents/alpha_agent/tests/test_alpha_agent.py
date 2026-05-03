from __future__ import annotations

from pathlib import Path

from agents.alpha_agent.config import AlphaAgentConfig
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


def test_workspace_manager_prepares_expected_layout(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "alpha")
    paths = manager.prepare(project_id="prj_abc123", task_id="tsk_def456")

    assert paths.workspace.is_dir()
    assert paths.artifacts.is_dir()
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
