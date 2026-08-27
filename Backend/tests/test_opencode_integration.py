from __future__ import annotations

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


def test_opencode_runner_env_scopes_home_and_injects_zen_key(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = OpenCodeWorkspaceRunner(config)
    runner.zen_api_key_override = "sk-zen-test-key"

    env = runner._env()
    home = str(config.opencode_home)
    assert env["HOME"] == home
    assert env["OPENCODE_CONFIG_DIR"] == str(config.opencode_home / ".config" / "opencode")
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert content["provider"]["opencode"]["options"]["apiKey"] == "sk-zen-test-key"


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
    reloaded = store.get_opencode(include_secret=False)
    assert reloaded["preferred_model"] == "mimo-v2.5-free"

    secret_view = store.get_opencode(include_secret=True)
    assert secret_view["api_key"] == "sk-zen-roundtrip"

    unknown = store.save_opencode(preferred_model="brand-new-model-x")
    assert unknown["preferred_model"] == "brand-new-model-x"

    cleared = store.clear_opencode_api_key()
    assert cleared["status"] == "logged_out"


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
