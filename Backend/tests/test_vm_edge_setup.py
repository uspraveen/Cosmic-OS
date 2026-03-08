from __future__ import annotations

from pathlib import Path

import bootstrap
import vm_edge_setup


def test_resolve_public_host_from_gateway_env(tmp_path) -> None:
    gateway_env = tmp_path / "gateway.env"
    gateway_env.write_text(
        "GATEWAY_HOST=0.0.0.0\n"
        "GATEWAY_PUBLIC_HOST=gateway.user.example.com\n",
        encoding="utf-8",
    )

    resolved = vm_edge_setup.resolve_public_host(gateway_env_path=gateway_env)

    assert resolved == "gateway.user.example.com"


def test_render_caddyfile_includes_expected_proxy_settings() -> None:
    rendered = vm_edge_setup.render_caddyfile(
        public_host="gateway.user.example.com",
        upstream="127.0.0.1:8080",
        email="ops@example.com",
    )

    assert vm_edge_setup.MANAGED_HEADER in rendered
    assert "email ops@example.com" in rendered
    assert "gateway.user.example.com {" in rendered
    assert "reverse_proxy 127.0.0.1:8080 {" in rendered
    assert "stream_timeout 24h" in rendered
    assert "stream_close_delay 5m" in rendered


def test_bootstrap_setup_vm_edge_invokes_script(monkeypatch, tmp_path) -> None:
    edge_script = tmp_path / "vm_edge_setup.py"
    gateway_env = tmp_path / "gateway.env"
    edge_script.write_text("# edge", encoding="utf-8")
    gateway_env.write_text("GATEWAY_PUBLIC_HOST=gateway.user.example.com\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)

    bootstrap.setup_vm_edge(
        edge_script,
        gateway_env,
        gateway_host="gateway.user.example.com",
        force=True,
        skip_if_unconfigured=True,
    )

    assert captured["command"] == [
        bootstrap.sys.executable,
        str(edge_script),
        "--gateway-env",
        str(gateway_env),
        "--gateway-host",
        "gateway.user.example.com",
        "--force",
        "--skip-if-unconfigured",
        "setup",
    ]
    assert captured["kwargs"] == {}
