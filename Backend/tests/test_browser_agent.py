"""Tests for the COSMIC Browser Agent specialist wrapper."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
BROWSER_USE_CANDIDATES = [
    BACKEND_ROOT.parent.parent / "cosmic-browser-use" / "cosmic-browser-use",
    BACKEND_ROOT.parent.parent / "agent-browser-index" / "cosmic-browser-use",
    BACKEND_ROOT.parent.parent / "Downloads" / "agent-browser-index" / "cosmic-browser-use",
]


def _browser_use_home() -> Path:
    for candidate in BROWSER_USE_CANDIDATES:
        if (candidate / "main.py").is_file():
            return candidate
    pytest.skip("cosmic-browser-use checkout not available on this machine")


@pytest.fixture()
def browser_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_USE_HOME", str(_browser_use_home()))
    from agents.browser_agent.agent import BrowserAgent
    from agents.browser_agent.config import BrowserAgentConfig

    config = BrowserAgentConfig.from_env()
    config.artifacts_root = tmp_path / "artifacts"
    agent = BrowserAgent(object(), config=config)  # redis client unused until run()
    try:
        yield agent
    finally:
        agent.auth = None


def _task(input_payload: dict, *, auth: dict | None = None):
    from shared.contracts import TaskEnvelope

    envelope = TaskEnvelope(
        task_id="t_browser_1",
        task_list_id="list",
        parent_task_id=None,
        session_id="sess",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/browser-agent:1.0.0",
        intent="browser.run",
        input=input_payload,
        idempotency_key="idem-1",
        signature="",
    )
    return envelope


def test_agent_card_loads(browser_agent):
    assert browser_agent.agent_id == "cosmic/browser-agent:1.0.0"
    assert browser_agent.max_concurrency == 1
    assert browser_agent.max_task_duration_sec == 900
    assert browser_agent.stream_key == "streams:cosmic/browser-agent:1.0.0"


def test_missing_goal_fails_cleanly(browser_agent):
    result = __import__("asyncio").run(browser_agent.handle_browser_run(_task({})))
    assert result.status == "failed"
    assert result.error.code == "INVALID_INPUT"


def test_credentials_from_auth(browser_agent):
    browser_agent.auth = {
        "vault": {
            "site_domain": "greenhouse.io",
            "site_url": "https://job-boards.greenhouse.io",
            "username": "me@example.com",
            "password": "hunter2",
            "totp_seed": "SEED",
        }
    }
    credentials = browser_agent._credentials_from_auth()
    assert credentials is not None
    entry = credentials["greenhouse.io"]
    assert entry["username"] == "me@example.com"
    assert entry["password"] == "hunter2"
    assert entry["totp_seed"] == "SEED"

    browser_agent.auth = {}
    assert browser_agent._credentials_from_auth() is None
    browser_agent.auth = {"vault": {"site_domain": "", "username": "", "password": ""}}
    assert browser_agent._credentials_from_auth() is None


def test_classify_ask_user_kind(browser_agent):
    assert browser_agent._classify_ask_user_kind("Please enter your password to continue.") == "password"
    assert browser_agent._classify_ask_user_kind("What is the verification code sent to your phone?") == "verification_code"
    assert browser_agent._classify_ask_user_kind("I see a CAPTCHA — can you solve it and tell me when done?") == "generic"


def test_attach_progress_screenshot_copies_and_stamps_artifact(browser_agent, tmp_path):
    from shared.contracts import TaskEnvelope

    source = tmp_path / "raw_step_004.jpg"
    source.write_bytes(b"fake-jpeg-bytes")
    task = TaskEnvelope(
        task_id="t_browser_shots",
        task_list_id="list",
        parent_task_id=None,
        session_id="sess",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/browser-agent:1.0.0",
        intent="browser.run",
        input={},
        idempotency_key="idem-shots",
        signature="",
    )

    screenshot = browser_agent._attach_progress_screenshot(task, {"screenshot_path": str(source), "step": 4})
    assert screenshot is not None
    assert screenshot["filename"] == "step-004.jpg"
    assert screenshot["mime"] == "image/jpeg"
    assert screenshot["artifact_id"].startswith("art_")
    assert len(screenshot["sha256"]) == 64
    assert screenshot["path"] == "runs/artifacts/t_browser_shots/browser_agent/previews/step-004.jpg"

    destination = browser_agent.artifacts_root / "t_browser_shots" / "browser_agent" / "previews" / "step-004.jpg"
    assert destination.is_file()
    assert destination.read_bytes() == b"fake-jpeg-bytes"


def test_attach_progress_screenshot_missing_path_returns_none(browser_agent):
    from shared.contracts import TaskEnvelope

    task = TaskEnvelope(
        task_id="t_browser_none",
        task_list_id="list",
        parent_task_id=None,
        session_id="sess",
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/browser-agent:1.0.0",
        intent="browser.run",
        input={},
        idempotency_key="idem-none",
        signature="",
    )
    assert browser_agent._attach_progress_screenshot(task, {"step": 1}) is None
    assert browser_agent._attach_progress_screenshot(task, {"screenshot_path": "/no/such/file.jpg", "step": 1}) is None


def test_run_goal_with_missing_home_fails_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_USE_HOME", str(tmp_path / "does-not-exist"))
    from agents.browser_agent.config import BrowserAgentConfig

    config = BrowserAgentConfig.from_env()
    with pytest.raises(RuntimeError, match="checkout not found"):
        config.ensure_import_path()
