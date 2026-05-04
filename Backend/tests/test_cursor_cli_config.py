from __future__ import annotations

import json

from shared.cursor_cli_config import ensure_cursor_cli_non_fast_config


def test_cursor_cli_config_preserves_auth_and_disables_composer_fast(tmp_path) -> None:
    config_path = tmp_path / ".cursor" / "cli-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "authInfo": {"accessToken": "keep-me"},
                "permissions": {"allow": ["Shell(ls)"], "deny": []},
                "modelParameters": {
                    "composer-2": [{"id": "fast", "value": "true"}],
                },
            }
        ),
        encoding="utf-8",
    )

    path, changed, config = ensure_cursor_cli_non_fast_config(tmp_path)

    assert path == config_path
    assert changed is True
    assert config["authInfo"] == {"accessToken": "keep-me"}
    assert config["permissions"]["allow"] == ["Shell(ls)"]
    assert config["modelParameters"]["composer-2"] == [{"id": "fast", "value": "false"}]


def test_cursor_cli_config_creates_minimal_non_fast_config(tmp_path) -> None:
    path, changed, config = ensure_cursor_cli_non_fast_config(tmp_path)

    assert path.exists()
    assert changed is True
    assert config["version"] == 1
    assert config["editor"] == {"vimMode": False}
    assert config["permissions"] == {"allow": [], "deny": []}
    assert config["modelParameters"]["composer-2"] == [{"id": "fast", "value": "false"}]
