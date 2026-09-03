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
    assert "--json" in command
    assert "--no-color" in command

    resumed = runner.build_command(
        paths=paths,
        prompt="Continue",
        resume_session_id="sess_12345678",
    )
    assert resumed[resumed.index("--resume") + 1] == "sess_12345678"


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
    assert "ANTHROPIC_API_KEY" not in env


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
