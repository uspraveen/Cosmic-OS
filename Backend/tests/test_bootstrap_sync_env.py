from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import bootstrap


def test_sync_repo_env_files_appends_missing_keys_without_overwriting_values(tmp_path) -> None:
    env_root = tmp_path / "envs"
    env_root.mkdir()
    example_path = env_root / "gateway.env.example"
    target_path = env_root / "gateway.env"

    example_path.write_text(
        "# Gateway settings\n"
        "EXISTING_TOKEN=<placeholder>\n"
        "GEMINI_API_KEY=<gemini-api-key>\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n",
        encoding="utf-8",
    )
    target_path.write_text(
        "EXISTING_TOKEN=real-token\n",
        encoding="utf-8",
    )

    bootstrap.sync_repo_env_files([env_root])

    rendered = target_path.read_text(encoding="utf-8")
    assert rendered.count("EXISTING_TOKEN=") == 1
    assert "EXISTING_TOKEN=real-token" in rendered
    assert "GEMINI_API_KEY=<gemini-api-key>" in rendered
    assert "PERPLEXITY_API_KEY=<perplexity-api-key>" in rendered


def test_sync_service_env_files_appends_missing_keys_without_overwriting_values(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "GATEWAY_HOST=0.0.0.0\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-token>\n"
        "GATEWAY_LOCAL_API_TOKEN=<local-token>\n"
        "WHATSAPP_BRIDGE_TOKEN=<bridge-token>\n"
        "GEMINI_API_KEY=<gemini-api-key>\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n",
        encoding="utf-8",
    )
    (backend_root / "model_router.env.example").write_text(
        "GROQ_API_KEY=<groq-api-key>\n"
        "CLASSIFIER_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )
    (bridge_dir / ".env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-token>\n"
        "WHATSAPP_BRIDGE_TOKEN=<bridge-token>\n"
        "WHATSAPP_AUTH_DIR=<auth-dir>\n",
        encoding="utf-8",
    )

    gateway_env_path = system_env_dir / "gateway.env"
    model_router_env_path = system_env_dir / "model-router.env"
    bridge_env_path = system_env_dir / "whatsapp-bridge.env"
    gateway_env_path.write_text(
        "GATEWAY_INTERNAL_TOKEN=shared-token\n"
        "GATEWAY_LOCAL_API_TOKEN=local-real\n"
        "WHATSAPP_BRIDGE_TOKEN=bridge-real\n"
        "EXISTING_KEY=keep-me\n",
        encoding="utf-8",
    )
    model_router_env_path.write_text(
        "GROQ_API_KEY=groq-real\n",
        encoding="utf-8",
    )
    bridge_env_path.write_text(
        "GATEWAY_INTERNAL_TOKEN=shared-token\n"
        "WHATSAPP_BRIDGE_TOKEN=bridge-real\n",
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        args = list(command)
        if args[:2] == ["cat", str(gateway_env_path)]:
            return SimpleNamespace(stdout=gateway_env_path.read_text(encoding="utf-8"), returncode=0)
        if args[:2] == ["cat", str(model_router_env_path)]:
            return SimpleNamespace(stdout=model_router_env_path.read_text(encoding="utf-8"), returncode=0)
        if args[:2] == ["cat", str(bridge_env_path)]:
            return SimpleNamespace(stdout=bridge_env_path.read_text(encoding="utf-8"), returncode=0)
        if args[0] == "install" and "-d" in args:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="", returncode=0)
        if args[0] == "install" and "-m" in args:
            src_path = Path(args[-2])
            dest_path = Path(args[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
            return SimpleNamespace(stdout="", returncode=0)
        raise AssertionError("Unexpected command: {0}".format(args))

    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(bootstrap, "DEFAULT_BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_SYSTEM_ENV_DIR", system_env_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_WHATSAPP_AUTH_DIR", PurePosixPath("/var/lib/cosmic/whatsapp/auth"))
    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "run", fake_run)

    bootstrap.sync_service_env_files(system_env_dir)

    gateway_rendered = gateway_env_path.read_text(encoding="utf-8")
    model_router_rendered = model_router_env_path.read_text(encoding="utf-8")
    bridge_rendered = bridge_env_path.read_text(encoding="utf-8")

    assert gateway_rendered.count("GATEWAY_LOCAL_API_TOKEN=") == 1
    assert "GATEWAY_LOCAL_API_TOKEN=local-real" in gateway_rendered
    assert "GEMINI_API_KEY=<gemini-api-key>" in gateway_rendered
    assert "PERPLEXITY_API_KEY=<perplexity-api-key>" in gateway_rendered
    assert "EXISTING_KEY=keep-me" in gateway_rendered

    assert model_router_rendered.count("GROQ_API_KEY=") == 1
    assert "GROQ_API_KEY=groq-real" in model_router_rendered
    assert "CLASSIFIER_MODEL=openai/gpt-oss-20b" in model_router_rendered

    assert bridge_rendered.count("WHATSAPP_BRIDGE_TOKEN=") == 1
    assert "WHATSAPP_BRIDGE_TOKEN=bridge-real" in bridge_rendered
    assert "WHATSAPP_AUTH_DIR=/var/lib/cosmic/whatsapp/auth" in bridge_rendered
