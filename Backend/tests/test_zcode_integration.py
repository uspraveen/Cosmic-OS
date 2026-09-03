from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agents.alpha_agent.config import AlphaAgentConfig
from agents.alpha_agent.instructions import (
    ZCODE_GLOBAL_INSTRUCTIONS_RELATIVE,
    ensure_zcode_global_instructions,
    render_global_instructions,
)
from agents.alpha_agent.workspace_manager import WorkspaceManager
from agents.alpha_agent.zcode_runner import (
    ZcodeWorkspaceRunner,
    normalize_zcode_model,
    qualify_zcode_model,
)
from agents.alpha_agent.agent import ALPHA_HARNESSES
from gateway.agent_auth_store import AgentAuthStore
from shared.zcode_cli import (
    build_zcode_config_update,
    ensure_zcode_cli_config,
    normalize_zcode_thinking,
    read_zcode_auth_state,
    zcode_cli_config_path,
    zcode_cli_env,
)


def _config(tmp_path: Path) -> AlphaAgentConfig:
    """Build the same isolated config the alpha agent tests use."""
    from agents.alpha_agent.tests.test_alpha_agent import _config

    return _config(tmp_path)


# ── Model / thinking normalization ───────────────────────────────────────────


def test_normalize_zcode_model_accepts_supported_spellings() -> None:
    assert normalize_zcode_model("glm-5.3") == "glm-5.3"
    assert normalize_zcode_model("GLM-5.3-Flash") == "glm-5.3-flash"
    assert normalize_zcode_model("zai/glm-5.3-flash") == "glm-5.3-flash"
    assert normalize_zcode_model("flash") == "glm-5.3-flash"
    assert normalize_zcode_model("glm 5.3") == "glm-5.3"
    assert normalize_zcode_model("auto") is None
    assert normalize_zcode_model(None) is None
    # Unsupported models never leak through to the CLI.
    assert normalize_zcode_model("glm-4.5-air") is None
    assert normalize_zcode_model("claude-opus") is None


def test_qualify_zcode_model_adds_provider_prefix() -> None:
    assert qualify_zcode_model("glm-5.3") == "zai/glm-5.3"
    assert qualify_zcode_model("glm-5.3-flash") == "zai/glm-5.3-flash"
    assert qualify_zcode_model("auto") is None


def test_normalize_zcode_thinking_maps_glms_ladder() -> None:
    assert normalize_zcode_thinking("low") == "low"
    assert normalize_zcode_thinking("high") == "high"
    assert normalize_zcode_thinking("max") == "max"
    assert normalize_zcode_thinking("auto") == "auto"
    # Reasoning-effort spellings from other harnesses map onto the same idea.
    assert normalize_zcode_thinking("medium") == "high"
    assert normalize_zcode_thinking("xhigh") == "max"
    assert normalize_zcode_thinking("minimal") == "low"
    assert normalize_zcode_thinking("nonsense") == "auto"


# ── CLI config file (the credential + model store) ──────────────────────────


def test_ensure_zcode_cli_config_writes_provider_and_model(tmp_path: Path) -> None:
    home = tmp_path / "zcode-home"
    result = ensure_zcode_cli_config(
        home,
        api_key=" test-key-123 ",
        preferred_model="GLM-5.3-Flash",
        thinking="high",
    )
    assert result["wrote"] is True
    assert result["has_api_key"] is True
    assert result["main_model"] == "zai/glm-5.3-flash"

    config = json.loads(zcode_cli_config_path(home).read_text(encoding="utf-8"))
    provider = config["provider"]["zai"]
    assert provider["kind"] == "anthropic"
    assert provider["options"]["apiKey"] == "test-key-123"
    assert provider["options"]["baseURL"] == "https://api.z.ai/api/anthropic"
    assert set(provider["models"]) == {"glm-5.3", "glm-5.3-flash"}
    for model in provider["models"].values():
        assert model["reasoning"]["variants"] == ["low", "high", "max"]
        assert model["reasoning"]["defaultVariant"] == "high"
    assert config["model"]["main"] == "zai/glm-5.3-flash"


def test_ensure_zcode_cli_config_is_idempotent_and_preserves_key(tmp_path: Path) -> None:
    home = tmp_path / "zcode-home"
    ensure_zcode_cli_config(home, api_key="secret-key", preferred_model="glm-5.3")
    first = zcode_cli_config_path(home).read_text(encoding="utf-8")

    # Same inputs → no disk write.
    result = ensure_zcode_cli_config(home, api_key="secret-key", preferred_model="glm-5.3")
    assert result["wrote"] is False
    assert zcode_cli_config_path(home).read_text(encoding="utf-8") == first

    # A key-less save (e.g. only switching models) must not drop the key.
    ensure_zcode_cli_config(home, preferred_model="glm-5.3-flash", thinking="max")
    config = json.loads(zcode_cli_config_path(home).read_text(encoding="utf-8"))
    assert config["provider"]["zai"]["options"]["apiKey"] == "secret-key"
    assert config["model"]["main"] == "zai/glm-5.3-flash"
    assert config["provider"]["zai"]["models"]["glm-5.3"]["reasoning"]["defaultVariant"] == "max"


def test_ensure_zcode_cli_config_merges_around_unknown_keys(tmp_path: Path) -> None:
    home = tmp_path / "zcode-home"
    config_path = zcode_cli_config_path(home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"plugins": {"enabledPlugins": {"x": True}}, "features": {"memory": True}}),
        encoding="utf-8",
    )
    ensure_zcode_cli_config(home, api_key="k1")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["plugins"]["enabledPlugins"] == {"x": True}
    assert config["features"]["memory"] is True
    assert config["provider"]["zai"]["options"]["apiKey"] == "k1"


def test_read_zcode_auth_state_reports_keyless_home(tmp_path: Path) -> None:
    home = tmp_path / "zcode-home"
    assert read_zcode_auth_state(home)["has_api_key"] is False
    ensure_zcode_cli_config(home, api_key="abc123")
    state = read_zcode_auth_state(home)
    assert state["configured"] is True
    assert state["has_api_key"] is True
    assert state["main_model"] == "zai/glm-5.3-flash"


def test_build_zcode_config_update_auto_thinking_keeps_model_default() -> None:
    update = build_zcode_config_update(preferred_model="glm-5.3", thinking="auto")
    provider = update["provider"]["zai"]
    assert provider["models"]["glm-5.3"]["reasoning"]["defaultVariant"] == "max"


# ── Runner ───────────────────────────────────────────────────────────────────


def test_zcode_runner_builds_headless_yolo_command(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = ZcodeWorkspaceRunner(config)
    paths = WorkspaceManager(config.alpha_root).prepare(project_id="prj_x", task_id="tsk_x")

    command = runner.build_command(paths=paths, prompt="Do the thing")
    assert command[0] == "zcode"
    assert command[command.index("--prompt") + 1] == "Do the thing"
    assert command[command.index("--mode") + 1] == "yolo"
    assert command[command.index("--cwd") + 1] == str(paths.workspace)
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--no-color" in command

    resumed = runner.build_command(
        paths=paths,
        prompt="Continue",
        resume_session_id="sess_12345678",
    )
    assert resumed[resumed.index("--resume") + 1] == "sess_12345678"

    # Pre-stream-json CLIs get the legacy whole-payload mode instead.
    legacy = runner.build_command(paths=paths, prompt="Legacy", stream_format=False)
    assert "--json" in legacy
    assert "--output-format" not in legacy


def test_zcode_runner_env_scopes_home_and_overrides_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = ZcodeWorkspaceRunner(config)

    env = runner._env("glm-5.3")
    assert env["HOME"] == str(config.zcode_home)
    assert env["ZCODE_MODEL"] == "zai/glm-5.3"
    assert env["ZCODE_BASE_URL"] == "https://api.z.ai/api/anthropic"
    # Credentials live in cli/config.json — ambient key vars are stripped so a
    # stray environment can never silently re-auth a run.
    for key in ("ZCODE_API_KEY", "ZAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert key not in env

    auto_env = runner._env(None)
    assert "ZCODE_MODEL" not in auto_env


def test_zcode_runner_extracts_session_and_response_from_json_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = ZcodeWorkspaceRunner(config)
    payload = {
        "sessionId": "sess_abc12345",
        "response": "All done.",
        "usage": {"totalTokens": 42},
    }
    stdout = json.dumps(payload) + "\n"
    assert runner._extract_json_payload(stdout) == payload
    assert runner._extract_native_session_id(stdout, payload) == "sess_abc12345"

    missing = runner._extract_native_session_id("{}", {})
    assert missing is None


def test_zcode_runner_seeds_global_instructions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = ZcodeWorkspaceRunner(config)
    paths = WorkspaceManager(config.alpha_root).prepare(project_id="prj_x", task_id="tsk_x")
    # The run path itself calls this before executing; call it directly since
    # the CLI is not installed in the test environment.
    result = ensure_zcode_global_instructions(zcode_home=config.zcode_home)
    target = config.zcode_home / ZCODE_GLOBAL_INSTRUCTIONS_RELATIVE
    assert result["wrote"] is True
    assert target.exists()
    assert "COSMIC Operator Instructions" in target.read_text(encoding="utf-8")
    # The per-CLI identity line is rendered for zcode.
    assert "ZCode CLI" in render_global_instructions(cli="zcode")
    assert paths.workspace.exists()


# ── Agent wiring ─────────────────────────────────────────────────────────────


def test_alpha_harnesses_include_zcode() -> None:
    assert "zcode" in ALPHA_HARNESSES
    assert ALPHA_HARNESSES[0] == "opencode"


def test_zcode_config_defaults_flow_from_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_WORKSPACE_ROOT", str(tmp_path / "alpha"))
    monkeypatch.delenv("ALPHA_ZCODE_HOME", raising=False)
    monkeypatch.delenv("ALPHA_ZCODE_MODEL", raising=False)
    monkeypatch.delenv("ALPHA_ZCODE_THINKING", raising=False)
    config = AlphaAgentConfig.from_env()
    assert config.zcode_home == (tmp_path / "alpha" / "homes" / "zcode")
    assert config.zcode_default_model == "glm-5.3-flash"
    assert config.zcode_default_thinking == "auto"

    monkeypatch.setenv("ALPHA_ZCODE_HOME", str(tmp_path / "custom-home"))
    monkeypatch.setenv("ALPHA_ZCODE_MODEL", "glm-5.3")
    monkeypatch.setenv("ALPHA_ZCODE_THINKING", "max")
    override = AlphaAgentConfig.from_env()
    assert override.zcode_home == tmp_path / "custom-home"
    assert override.zcode_default_model == "glm-5.3"
    assert override.zcode_default_thinking == "max"


# ── Gateway auth store ───────────────────────────────────────────────────────


def test_agent_auth_store_zcode_round_trip(tmp_path: Path) -> None:
    store = AgentAuthStore(tmp_path / "auth.db")
    store.initialize()
    try:
        defaults = store.get_zcode()
        assert defaults["preferred_model"] == "glm-5.3-flash"
        assert defaults["reasoning_effort"] == "auto"
        assert defaults["status"] == "not_configured"

        saved = store.save_zcode(
            preferred_model="GLM 5.3",
            thinking="max",
            status="authenticated",
            login_required_reason="",
            has_api_key=True,
        )
        assert saved["preferred_model"] == "glm-5.3"
        assert saved["reasoning_effort"] == "max"
        assert saved["status"] == "authenticated"
        assert saved["has_api_key"] is True

        # Unsupported ids fall back to the currently saved model instead of
        # reaching the CLI; with no prior model at all, the default wins.
        assert store.save_zcode(preferred_model="claude-opus")["preferred_model"] == "glm-5.3"
        fresh = AgentAuthStore(tmp_path / "auth-fresh.db")
        fresh.initialize()
        try:
            assert fresh.save_zcode(preferred_model="claude-opus")["preferred_model"] == "glm-5.3-flash"
        finally:
            fresh.close()

        cleared = store.clear_zcode_auth()
        assert cleared["status"] == "logged_out"
        assert cleared["has_api_key"] is False
    finally:
        store.close()


# ── Env builder ──────────────────────────────────────────────────────────────


def test_zcode_cli_env_model_override_and_key_stripping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "ambient-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-anthropic")
    env = zcode_cli_env(tmp_path, model="glm-5.3-flash")
    assert env["HOME"] == str(tmp_path)
    assert env["ZCODE_MODEL"] == "zai/glm-5.3-flash"
    assert "ZAI_API_KEY" not in env
    # No config in the home yet: the fallback key slot must stay empty rather
    # than inherit an ambient value.
    assert "ANTHROPIC_API_KEY" not in env


def test_zcode_cli_env_injects_config_key_for_model_override(tmp_path: Path) -> None:
    ensure_zcode_cli_config(tmp_path, api_key="config-stored-key")

    override_env = zcode_cli_env(tmp_path, model="glm-5.3")
    assert override_env["ANTHROPIC_API_KEY"] == "config-stored-key"
    assert override_env["ZCODE_MODEL"] == "zai/glm-5.3"

    # Without a model override the CLI reads the key from its own config, and
    # ambient keys must not ride along.
    plain_env = zcode_cli_env(tmp_path)
    assert "ANTHROPIC_API_KEY" not in plain_env
    assert "ZCODE_MODEL" not in plain_env


def test_zcode_provider_api_key_reads_config_only(tmp_path: Path) -> None:
    from shared.zcode_cli import zcode_provider_api_key

    assert zcode_provider_api_key(tmp_path) == ""
    ensure_zcode_cli_config(tmp_path, api_key="key-abc")
    assert zcode_provider_api_key(tmp_path) == "key-abc"


def test_ensure_zcode_home_writable_passes_on_fresh_dir(tmp_path: Path) -> None:
    from shared.zcode_cli import ensure_zcode_home_writable, zcode_home_writable

    home = tmp_path / "homes" / "zcode"
    # A not-yet-created home must not read as blocked (fresh installs).
    assert zcode_home_writable(home) is True
    assert ensure_zcode_home_writable(home) is None
    assert (home / ".zcode").is_dir()
    assert zcode_home_writable(home) is True


def test_zcode_home_writable_flags_unwritable_dir(tmp_path: Path) -> None:
    import os

    from shared.zcode_cli import ensure_zcode_home_writable, zcode_home_writable

    if os.name != "posix":
        # Windows enforcement of directory write bits is not reliable.
        return
    home = tmp_path / "homes" / "zcode"
    home.mkdir(parents=True)
    home.chmod(0o555)
    try:
        assert zcode_home_writable(home) is False
        reason = ensure_zcode_home_writable(home)
        assert reason is not None
        assert "not writable" in reason
        assert str(home) in reason
    finally:
        home.chmod(0o755)


def test_ensure_zcode_cli_config_creates_missing_home(tmp_path: Path) -> None:
    from shared.zcode_cli import ensure_zcode_cli_config, read_zcode_auth_state

    home = tmp_path / "homes" / "zcode"
    result = ensure_zcode_cli_config(home, api_key="key-123")
    assert result["wrote"] is True
    assert result["has_api_key"] is True
    state = read_zcode_auth_state(home)
    assert state["has_api_key"] is True


def test_zcode_progress_lines_map_stream_events() -> None:
    from agents.alpha_agent.zcode_runner import _progress_line_for_event

    assert _progress_line_for_event({"type": "turn.started", "turnNumber": 0}) == "── turn 1 started ──"
    request_line = _progress_line_for_event({
        "type": "model_request_completed",
        "usage": {"inputTokens": 22561, "outputTokens": 360},
        "durationMs": 13065,
    })
    assert "in 22561 tok" in request_line
    assert "out 360 tok" in request_line
    assert "13.1s" in request_line

    started_tool = _progress_line_for_event({
        "type": "tool.updated",
        "kind": "tool_input_start",
        "toolName": "Write",
        "input": {"file_path": "/tmp/zz.txt", "content": "hi"},
    })
    assert started_tool == "→ Write /tmp/zz.txt"
    assert _progress_line_for_event({
        "type": "tool.updated",
        "status": "tool_result_committed",
        "toolName": "Write",
    }) == "✓ Write"
    # queued/closed transitions stay silent
    assert _progress_line_for_event({"type": "tool.updated", "status": "tool_queued"}) is None

    completed = _progress_line_for_event({
        "type": "turn.completed",
        "resultType": "success",
        "tokenCount": 22921,
        "toolCallCount": 3,
        "response": "All finished properly.",
    })
    assert "22921 tok" in completed
    assert "3 tools" in completed
    assert "All finished properly." in completed

    # Streaming deltas and bookkeeping events never reach the card.
    for skipped in (
        {"type": "model.streaming", "kind": "delta", "delta": "abc"},
        {"type": "checkpoint.created", "checkpointId": "cp_1"},
        {"type": "session.titleUpdated", "title": "x"},
    ):
        assert _progress_line_for_event(skipped) is None


def test_zcode_runner_extracts_result_from_stream_stdout(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = ZcodeWorkspaceRunner(config)
    events = [
        json.dumps({"eventId": "e1", "payload": {"type": "turn.started", "turnNumber": 0}}),
        json.dumps({"eventId": "e2", "payload": {"type": "model.streaming", "delta": "hi", "kind": "delta"}}),
        json.dumps({
            "type": "result",
            "sessionId": "sess_stream999",
            "response": "Streamed answer.",
            "usage": {"totalTokens": 7},
        }),
    ]
    stdout = "\n".join(events) + "\n"
    payload = runner._extract_json_payload(stdout)
    assert payload is not None
    assert payload.get("response") == "Streamed answer."
    assert runner._extract_native_session_id(stdout, payload) == "sess_stream999"


def test_zcode_progress_lines_map_scheduled_and_result_tool_shapes() -> None:
    from agents.alpha_agent.zcode_runner import _progress_line_for_event

    scheduled = _progress_line_for_event({
        "type": "tool.updated",
        "kind": "scheduled",
        "toolName": "Write",
        "input": {"file_path": "/tmp/zz_mapper.txt", "content": "hi"},
    })
    assert scheduled == "→ Write /tmp/zz_mapper.txt"

    assert _progress_line_for_event({
        "type": "tool.updated",
        "kind": "started",
        "toolName": "Write",
    }) is None

    ok_result = _progress_line_for_event({
        "type": "tool.updated",
        "kind": "result",
        "toolName": "Write",
        "result": {"success": True, "content": "File created"},
    })
    assert ok_result == "✓ Write"

    failed = _progress_line_for_event({
        "type": "tool.updated",
        "kind": "result",
        "toolName": "Bash",
        "result": {"success": False, "content": "exit code 1: no such file"},
    })
    assert failed is not None
    assert failed.startswith("✗ Bash")
    assert "exit code 1" in failed
