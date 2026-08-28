from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agents.alpha_agent.config import AlphaAgentConfig
from agents.alpha_agent.instructions import (
    OPENCODE_GLOBAL_INSTRUCTIONS_RELATIVE,
    render_global_instructions,
)
from agents.alpha_agent.opencode_runner import (
    OpenCodeWorkspaceRunner,
    normalize_opencode_model,
)
from agents.alpha_agent.workspace_manager import WorkspaceManager
from gateway.agent_auth_store import AgentAuthStore
from gateway.runtime import GatewayRuntime


def _config(tmp_path: Path):
    """Build the same isolated config the alpha agent tests use."""
    from agents.alpha_agent.tests.test_alpha_agent import _config

    return _config(tmp_path)


def test_normalize_opencode_model_prefixes_bare_zen_ids() -> None:
    assert normalize_opencode_model("mimo-v2.5-free") == "opencode/mimo-v2.5-free"
    assert normalize_opencode_model("opencode/gpt-5.5") == "opencode/gpt-5.5"
    assert normalize_opencode_model("auto") is None
    assert normalize_opencode_model(None) is None
    assert normalize_opencode_model("") is None


def test_opencode_runner_builds_headless_auto_run_command(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OpenCodeWorkspaceRunner(config)
    paths = WorkspaceManager(config.alpha_root).prepare(project_id="prj_x", task_id="tsk_x")

    plain = runner.build_command(paths=paths, prompt="Do the thing")
    assert plain[1] == "run"
    assert "--dir" in plain and str(paths.workspace) in plain
    assert "--auto" in plain
    assert plain[-1] == "Do the thing"

    resumed = runner.build_command(
        paths=paths,
        prompt="Continue",
        model="mimo-v2.5-free",
        resume_session_id="ses_1234567890abcdef",
        json_format=True,
    )
    assert resumed[resumed.index("--session") + 1] == "ses_1234567890abcdef"
    assert resumed[resumed.index("--model") + 1] == "opencode/mimo-v2.5-free"
    assert resumed[resumed.index("--format") + 1] == "json"


def test_opencode_runner_env_scopes_home_and_injects_provider_keys(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OpenCodeWorkspaceRunner(config)
    runner.provider_keys = {"opencode": "sk-zen-test-key", "xai": "xai-key-1"}

    env = runner._env()
    home = str(config.opencode_home)
    assert env["HOME"] == home
    assert env["OPENCODE_CONFIG_DIR"] == str(config.opencode_home / ".config" / "opencode")
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    providers = content["provider"]
    assert providers["opencode"]["options"]["apiKey"] == "sk-zen-test-key"
    assert providers["xai"]["options"]["apiKey"] == "xai-key-1"


def test_opencode_runner_keyless_is_healthy_and_variant_flows_through(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OpenCodeWorkspaceRunner(config)

    # Keyless is a supported state (free Zen tier): no config content injected.
    env = runner._env()
    assert "OPENCODE_CONFIG_CONTENT" not in env

    paths = WorkspaceManager(config.alpha_root).prepare(project_id="prj_v", task_id="tsk_v")
    command = runner.build_command(
        paths=paths,
        prompt="hi",
        model="mimo-v2.5-free",
        variant="high",
    )
    assert "--variant" in command
    assert command[command.index("--variant") + 1] == "high"
    # 'auto' (or garbage) means: omit the flag, defer to provider defaults.
    auto_command = runner.build_command(paths=paths, prompt="hi", variant="auto")
    assert "--variant" not in auto_command


def test_opencode_global_instructions_render_and_write(tmp_path: Path) -> None:
    rendered = render_global_instructions(cli="opencode")
    assert "OpenCode CLI" in rendered
    assert "`opencode run`" in rendered
    # Distinct identity lines per harness.
    codex_rendered = render_global_instructions(cli="codex")
    cursor_rendered = render_global_instructions(cli="cursor")
    assert rendered != codex_rendered != cursor_rendered != rendered

    from agents.alpha_agent.instructions import ensure_opencode_global_instructions

    result = ensure_opencode_global_instructions(opencode_home=tmp_path / "oc-home")
    target = tmp_path / "oc-home" / OPENCODE_GLOBAL_INSTRUCTIONS_RELATIVE
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == rendered
    # Idempotent second write.
    again = ensure_opencode_global_instructions(opencode_home=tmp_path / "oc-home")
    assert again["wrote"] is False
    assert result["path"] == str(target)


def test_agent_auth_store_opencode_roundtrip_and_aliases(tmp_path: Path) -> None:
    store = AgentAuthStore(tmp_path / "credentials.db")
    store.initialize()

    settings = store.save_opencode(
        api_key="sk-zen-roundtrip",
        preferred_model="mimo v2.5 pro",
    )
    assert settings["provider"] == "opencode"
    assert settings["has_api_key"] is True
    assert settings["connected_providers"] == ["opencode"]
    reloaded = store.get_opencode(include_secret=False)
    assert reloaded["preferred_model"] == "mimo-v2.5-free"

    # Multi-provider: connect xai + anthropic, then disconnect one.
    store.connect_opencode_provider(provider_id="xai", api_key="xai-key-1")
    store.connect_opencode_provider(provider_id="anthropic", api_key="sk-ant-1")
    connected = store.get_opencode(include_secret=False)
    assert connected["connected_providers"] == ["anthropic", "opencode", "xai"]

    secret_view = store.get_opencode(include_secret=True)
    assert secret_view["provider_keys"]["xai"] == "xai-key-1"
    assert secret_view["provider_keys"]["opencode"] == "sk-zen-roundtrip"

    store.disconnect_opencode_provider(provider_id="xai")
    still_connected = store.get_opencode(include_secret=False)
    assert still_connected["connected_providers"] == ["anthropic", "opencode"]
    cleared_all = store.clear_opencode_api_key()
    assert cleared_all["connected_providers"] == ["anthropic"]

    unknown = store.save_opencode(preferred_model="brand-new-model-x")
    assert unknown["preferred_model"] == "brand-new-model-x"


def test_gateway_parses_live_opencode_models_output() -> None:
    text = "\n".join(
        [
            "opencode/big-pickle",
            "opencode/mimo-v2.5-free",
            "  anthropic/claude-opus-4-8  ",
            "openai/gpt-5.5",
            "",
            "banner noise without a slash",
            "not-a-pair / spaced",
            "openai/gpt-5.5",  # duplicate must be dropped
            "@weird/$format",
        ]
    )
    grouped = GatewayRuntime._parse_opencode_models_output(text)
    assert grouped == {
        "opencode": ["big-pickle", "mimo-v2.5-free"],
        "anthropic": ["claude-opus-4-8"],
        "openai": ["gpt-5.5"],
    }
    assert GatewayRuntime._parse_opencode_models_output("") == {}


def test_opencode_is_ready_without_any_zen_key() -> None:
    """Free Zen models run keyless (verified live on a fresh HOME).

    CLI present => authenticated, regardless of key state; missing CLI is
    the only setup problem. A saved/cleared optional key must never flip
    OpenCode to login_required.
    """
    runtime = object.__new__(GatewayRuntime)

    ready = runtime._effective_opencode_status(
        {"has_api_key": False}, {"available": True}
    )
    assert ready == {"status": "authenticated", "login_required_reason": ""}

    with_key = runtime._effective_opencode_status(
        {"has_api_key": True}, {"available": True}
    )
    assert with_key["status"] == "authenticated"

    missing_cli = runtime._effective_opencode_status(
        {"has_api_key": False}, {"available": False, "reason": "opencode_cli_missing"}
    )
    assert missing_cli["status"] == "relogin_required"
    assert missing_cli["login_required_reason"] == "opencode_cli_missing"


def test_opencode_cli_status_surfaces_authenticated_for_alpha_auth_walk(tmp_path: Path) -> None:
    """Regression: the Alpha pre-execution auth walk gates on cli.authenticated.

    _opencode_cli_status must report authenticated whenever the CLI is
    available (keyless free tier), otherwise Alpha silently skips OpenCode
    every run — status says "authenticated" but the cli block fails the ready
    check, and the task rotates to codex/cursor.
    """
    from agents.alpha_agent.agent import AlphaAgent

    runtime = object.__new__(GatewayRuntime)

    class _Cfg:
        alpha_opencode_home = tmp_path / "opencode-home"

    runtime.config = _Cfg()

    async def _version_ok(args: list[str], *, timeout_sec: float, with_keys: dict[str, str] | None = None):
        del timeout_sec, with_keys
        assert args == ["--version"]
        return {"ok": True, "available": True, "returncode": 0, "stdout": "1.18.23", "stderr": ""}

    async def _missing(args: list[str], *, timeout_sec: float, with_keys: dict[str, str] | None = None):
        del args, timeout_sec, with_keys
        return {
            "ok": False,
            "available": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "reason": "opencode_cli_missing",
        }

    runtime._run_opencode_command = _version_ok
    status = asyncio.run(runtime._opencode_cli_status())
    assert status["authenticated"] is True
    assert status["version"] == "1.18.23"

    runtime._run_opencode_command = _missing
    missing = asyncio.run(runtime._opencode_cli_status())
    assert missing["authenticated"] is False

    agent = object.__new__(AlphaAgent)
    ready_payload = {"status": "authenticated", "login_required_reason": "", "cli": status}
    assert agent._provider_status_is_ready(ready_payload) is True
    not_ready_payload = {"status": "relogin_required", "login_required_reason": "opencode_cli_missing", "cli": missing}
    assert agent._provider_status_is_ready(not_ready_payload) is False


def test_track_partial_stream_pins_alpha_anchor_at_delegation_boundary() -> None:
    """Regression: Alpha console anchors pin at the delegation boundary.

    The orchestrator's tool_loop progress event carries specialist_delegations
    right after its tools ran and before the next turn streams text. Pinning
    there places the inline console between the pre-delegation paragraph and
    the post-delegation narration instead of wherever the first terminal
    event happened to arrive.
    """
    from gateway.runtime import ActiveRequest

    runtime = object.__new__(GatewayRuntime)
    state = ActiveRequest(
        request_id="req_1",
        session_id="sess_1",
        channel="desktop:desk_1",
        route="opus",
    )

    runtime._track_partial_stream(state, {"type": "response.chunk", "content": "Let me fire Alpha with the full brief."})
    pinned = len("Let me fire Alpha with the full brief.")

    runtime._track_partial_stream(
        state,
        {
            "type": "task.progress",
            "status": "tool_loop",
            "specialist_delegations": [
                {"intent": "alpha.execute", "task_id": "tsk_alpha_1"},
            ],
        },
    )
    assert state.alpha_console_anchors == {"tsk_alpha_1": pinned}

    runtime._track_partial_stream(state, {"type": "response.chunk", "content": "\n\nLet me read the blog post Alpha wrote."})
    runtime._track_partial_stream(
        state,
        {
            "type": "task.progress",
            "task_id": "tsk_parent",
            "codex_terminal": {
                "stream": "system",
                "event_type": "opencode.exec.started",
                "text": "opencode run --auto started",
                "task_id": "tsk_alpha_1",
            },
        },
    )
    assert state.alpha_terminal_log[-1]["stream_offset"] == pinned

    # A terminal event that arrived during a blocking delegation already
    # pinned a (correct, pre-tool) anchor: the later delegation event must
    # not clobber it.
    runtime._track_partial_stream(
        state,
        {
            "type": "task.progress",
            "task_id": "tsk_parent",
            "codex_terminal": {
                "stream": "system",
                "event_type": "opencode.exec.started",
                "text": "started",
                "task_id": "tsk_alpha_2",
            },
        },
    )
    blocking_pinned = state.alpha_console_anchors["tsk_alpha_2"]
    runtime._track_partial_stream(
        state,
        {
            "type": "task.progress",
            "status": "tool_loop",
            "specialist_delegations": [
                {"intent": "alpha.execute", "task_id": "tsk_alpha_2"},
            ],
        },
    )
    assert state.alpha_console_anchors["tsk_alpha_2"] == blocking_pinned


def test_gateway_shapes_zen_catalog_and_semver_compare() -> None:
    payload = GatewayRuntime._shape_opencode_models_payload(
        {
            "models": ["gpt-5.5", "big-pickle", "mimo-v2.5-free"],
            "fetched_at": "2026-08-26T00:00:00Z",
        },
        "live",
    )
    ids = [model["id"] for model in payload["models"]]
    assert set(ids) == {"gpt-5.5", "big-pickle", "mimo-v2.5-free"}
    free_flags = {model["id"]: model["free"] for model in payload["models"]}
    assert free_flags["mimo-v2.5-free"] is True
    assert free_flags["big-pickle"] is True
    assert free_flags["gpt-5.5"] is False
    # Free models sort first (alphabetically within the free tier).
    assert ids[:2] == ["big-pickle", "mimo-v2.5-free"]
    assert payload["models"][0]["qualified"] == "opencode/big-pickle"

    assert GatewayRuntime._extract_semver("opencode 1.18.23 (abc)") == "1.18.23"
    runtime = object.__new__(GatewayRuntime)
    assert runtime._semver_tuple("1.18.23") > runtime._semver_tuple("1.9.99")
    assert runtime._semver_tuple("") == ()
