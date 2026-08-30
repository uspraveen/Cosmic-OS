from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CURSOR_CLI_CONFIG_RELATIVE_PATH = Path(".cursor") / "cli-config.json"
DEFAULT_CURSOR_MODEL = "cursor-grok-4.6-high"
DEFAULT_CURSOR_MODEL_DISPLAY = "Cursor Grok 4.6"
# Composer still uses an explicit Fast parameter; keep those models pinned to Standard.
NON_FAST_CURSOR_MODELS = ("composer-2.5", "composer-2")
FAST_PARAMETER = {"id": "fast", "value": "false"}


def cursor_cli_config_path(cursor_home: str | Path) -> Path:
    return Path(cursor_home).expanduser() / CURSOR_CLI_CONFIG_RELATIVE_PATH


def _non_fast_parameters(value: Any) -> list[dict[str, Any]]:
    parameters = [dict(parameter) for parameter in value if isinstance(parameter, dict)] if isinstance(value, list) else []
    fast_parameter: dict[str, Any] | None = None
    for parameter in parameters:
        if parameter.get("id") == "fast":
            fast_parameter = parameter
            break
    if fast_parameter is None:
        parameters.append(dict(FAST_PARAMETER))
    else:
        fast_parameter["value"] = "false"
    return parameters


def _default_model_parameters() -> list[dict[str, Any]]:
    # Grok effort/fast are encoded in the model id (…-high vs …-high-fast), not a Fast param.
    if DEFAULT_CURSOR_MODEL.startswith("composer-"):
        return _non_fast_parameters(None)
    return []


def _cursor_model_config(value: Any) -> dict[str, Any]:
    model = dict(value) if isinstance(value, dict) else {}
    model["modelId"] = DEFAULT_CURSOR_MODEL
    model["displayModelId"] = DEFAULT_CURSOR_MODEL
    model["displayName"] = DEFAULT_CURSOR_MODEL_DISPLAY
    model["displayNameShort"] = DEFAULT_CURSOR_MODEL_DISPLAY
    if not isinstance(model.get("aliases"), list):
        model["aliases"] = []
    model["maxMode"] = False
    return model


def ensure_cursor_cli_non_fast_config(cursor_home: str | Path) -> tuple[Path, bool, dict[str, Any]]:
    """Preserve Cursor CLI auth/config while pinning the Cosmic default model (non-Fast)."""

    path = cursor_cli_config_path(cursor_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {}
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                config = parsed
        except json.JSONDecodeError:
            config = {}

    original = json.dumps(config, sort_keys=True, separators=(",", ":"))
    if not isinstance(config.get("version"), int):
        config["version"] = 1

    editor = config.get("editor")
    if not isinstance(editor, dict):
        editor = {}
    editor.setdefault("vimMode", False)
    config["editor"] = editor

    permissions = config.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    if not isinstance(permissions.get("allow"), list):
        permissions["allow"] = []
    if not isinstance(permissions.get("deny"), list):
        permissions["deny"] = []
    config["permissions"] = permissions

    config["model"] = _cursor_model_config(config.get("model"))
    config["selectedModel"] = {
        "modelId": DEFAULT_CURSOR_MODEL,
        "parameters": _default_model_parameters(),
    }
    config["hasChangedDefaultModel"] = True

    model_parameters = config.get("modelParameters")
    if not isinstance(model_parameters, dict):
        model_parameters = {}
    for model_id in NON_FAST_CURSOR_MODELS:
        model_parameters[model_id] = _non_fast_parameters(model_parameters.get(model_id))
    config["modelParameters"] = model_parameters

    rendered = json.dumps(config, indent=2, sort_keys=False) + "\n"
    changed = original != json.dumps(config, sort_keys=True, separators=(",", ":"))
    if changed or not path.exists():
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
        ) as handle:
            handle.write(rendered)
            temp_name = handle.name
        os.replace(temp_name, path)
    return path, changed, config
