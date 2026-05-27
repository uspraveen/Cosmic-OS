from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CURSOR_CLI_CONFIG_RELATIVE_PATH = Path(".cursor") / "cli-config.json"
DEFAULT_CURSOR_MODEL = "composer-2.5"
NON_FAST_CURSOR_MODELS = ("composer-2.5", "composer-2")


def cursor_cli_config_path(cursor_home: str | Path) -> Path:
    return Path(cursor_home).expanduser() / CURSOR_CLI_CONFIG_RELATIVE_PATH


def ensure_cursor_cli_non_fast_config(cursor_home: str | Path) -> tuple[Path, bool, dict[str, Any]]:
    """Preserve Cursor CLI auth/config while disabling Composer Fast parameters."""

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

    config["model"] = DEFAULT_CURSOR_MODEL

    model_parameters = config.get("modelParameters")
    if not isinstance(model_parameters, dict):
        model_parameters = {}
    for model_id in NON_FAST_CURSOR_MODELS:
        parameters = model_parameters.get(model_id)
        if not isinstance(parameters, list):
            parameters = []

        fast_parameter: dict[str, Any] | None = None
        for parameter in parameters:
            if isinstance(parameter, dict) and parameter.get("id") == "fast":
                fast_parameter = parameter
                break
        if fast_parameter is None:
            parameters.append({"id": "fast", "value": "false"})
        else:
            fast_parameter["value"] = "false"
        model_parameters[model_id] = parameters
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
