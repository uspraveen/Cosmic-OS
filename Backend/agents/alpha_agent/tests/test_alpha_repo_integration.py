"""Unit tests for Alpha's connected-repository wiring (pure helpers).

The heavy git work is covered by test_repo_sync.py; this file covers the
repo reference detection, prompt rendering, and gateway resolution helpers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agents.alpha_agent.agent import AlphaAgent
from agents.alpha_agent.config import AlphaAgentConfig
from agents.alpha_agent.project_registry import ProjectRegistry
from agents.alpha_agent.repo_sync import RepoCheckout, RepoSnapshot
from shared.contracts import TaskEnvelope


class _FakeRedis:
    async def get(self, key: str):
        del key
        return None

    async def incr(self, key: str) -> int:
        del key
        return 1

    async def xadd(self, stream: str, fields: dict, **kwargs) -> str:
        del stream, fields, kwargs
        return "0-1"

    async def rpush(self, key: str, value: object) -> int:
        del key, value
        return 1


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
        opencode_home=tmp_path / "alpha" / "homes" / "opencode",
        zcode_home=tmp_path / "alpha" / "homes" / "zcode",
        codex_sandbox="workspace-write",
        codex_timeout_sec=14400.0,
        codex_default_model="",
        cursor_timeout_sec=14400.0,
        cursor_default_model="",
        cursor_init_timeout_sec=180.0,
        opencode_timeout_sec=14400.0,
        opencode_default_model="mimo-v2.5-free",
        zcode_timeout_sec=14400.0,
        zcode_default_model="glm-5.3-flash",
        zcode_default_thinking="auto",
        zen_api_key="",
        cli_idle_check_sec=300.0,
        repos_root=tmp_path / "repos",
    )


def _task(payload: dict) -> TaskEnvelope:
    return TaskEnvelope(
        task_id="tsk_test",
        task_list_id="list_test",
        session_id="sess_test",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/alpha-agent:1.0.0",
        intent="alpha.execute",
        input=payload,
        idempotency_key="idem_test",
        signature="",
    )


def test_repo_ref_prefers_explicit_repo_url(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    ref = agent._repo_ref_from_task(
        _task({"repo_url": "https://github.com/acme/site.git", "goal": "update"}),
        None,
    )
    assert ref == "https://github.com/acme/site.git"


def test_repo_ref_falls_back_to_project_repo_url(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    registry = ProjectRegistry(tmp_path / "projects.db")
    project = registry.create_project(repo_url="https://github.com/acme/site.git")

    ref = agent._repo_ref_from_task(_task({"goal": "update"}), project)

    assert ref == "https://github.com/acme/site.git"


def test_repo_ref_extracts_github_url_from_goal(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    ref = agent._repo_ref_from_task(
        _task({"goal": "Ship a change in https://github.com/acme/site"}),
        None,
    )
    assert ref == "https://github.com/acme/site"


def test_repo_ref_returns_none_without_any_reference(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    assert agent._repo_ref_from_task(_task({"goal": "write a poem"}), None) is None


def test_render_repo_block_describes_sync_state(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    snapshot = RepoSnapshot(
        local_path="/var/lib/cosmic/alpha/repos/acme/site",
        branch="main",
        behind=0,
        ahead=0,
        dirty=False,
        untracked=0,
        last_commit={
            "sha": "abc123",
            "message": "feat: ship it",
            "author": "praveen",
            "committed_at": "2026-08-30T10:00:00+00:00",
        },
    )
    checkout = RepoCheckout(
        repo_row_id="ghr_1",
        full_name="acme/site",
        local_path="/var/lib/cosmic/alpha/repos/acme/site",
        action="up_to_date",
        snapshot=snapshot,
    )

    lines = agent._render_repo_block(checkout, "acme/site")

    assert any("in sync with origin" in line for line in lines)
    assert any("abc123" in line for line in lines)
    assert any("Never rewrite history" in line for line in lines)


def test_render_repo_block_marks_divergence(tmp_path: Path) -> None:
    agent = AlphaAgent(_FakeRedis(), config=_config(tmp_path))
    snapshot = RepoSnapshot("/p", "main", 1, 1, False, 0, None)
    checkout = RepoCheckout("ghr_1", "acme/site", "/p", "diverged", snapshot)

    lines = agent._render_repo_block(checkout, "acme/site")

    assert any("DIVERGED" in line for line in lines)


def test_resolve_connected_repo_returns_none_without_token(tmp_path: Path) -> None:
    cfg = AlphaAgentConfig(
        redis_url="redis://127.0.0.1:6379/0",
        gateway_url="http://127.0.0.1:8080",
        gateway_internal_token="",
        orchestrator_url="http://127.0.0.1:8743",
        orchestrator_internal_token="",
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
        opencode_home=tmp_path / "alpha" / "homes" / "opencode",
        zcode_home=tmp_path / "alpha" / "homes" / "zcode",
        codex_sandbox="workspace-write",
        codex_timeout_sec=14400.0,
        codex_default_model="",
        cursor_timeout_sec=14400.0,
        cursor_default_model="",
        cursor_init_timeout_sec=180.0,
        opencode_timeout_sec=14400.0,
        opencode_default_model="mimo-v2.5-free",
        zcode_timeout_sec=14400.0,
        zcode_default_model="glm-5.3-flash",
        zcode_default_thinking="auto",
        zen_api_key="",
        cli_idle_check_sec=300.0,
    )
    agent = AlphaAgent(_FakeRedis(), config=cfg)

    assert asyncio.run(agent._resolve_connected_repo("acme/site")) is None
