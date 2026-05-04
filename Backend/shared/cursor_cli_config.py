from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


CURSOR_CLI_CONFIG_RELATIVE_PATH = Path(".cursor") / "cli-config.json"


def cursor_cli_config_path(cursor_home: str | Path) -> Path:
    return Path(cursor_home).expanduser() / CURSOR_CLI_CONFIG_RELATIVE_PATH


def ensure_cursor_cli_non_fast_config(cursor_home: str | Path) -> tuple[Path, bool, dict[str, Any]]:
    """Preserve Cursor CLI auth/config while disabling the Composer 2 Fast parameter."""

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

    model_parameters = config.get("modelParameters")
    if not isinstance(model_parameters, dict):
        model_parameters = {}
    composer_2_parameters = model_parameters.get("composer-2")
    if not isinstance(composer_2_parameters, list):
        composer_2_parameters = []

    fast_parameter: dict[str, Any] | None = None
    for parameter in composer_2_parameters:
        if isinstance(parameter, dict) and parameter.get("id") == "fast":
            fast_parameter = parameter
            break
    if fast_parameter is None:
        composer_2_parameters.append({"id": "fast", "value": "false"})
    else:
        fast_parameter["value"] = "false"
    model_parameters["composer-2"] = composer_2_parameters
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
