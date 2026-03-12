from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import bootstrap


def test_sync_repo_env_files_appends_missing_keys_without_overwriting_values(tmp_path) -> None:
    env_root = tmp_path / "envs"
    env_root.mkdir()
    example_path = env_root / "gateway.env.example"
    target_path = env_root / "gateway.env"

    example_path.write_text(
        "# Gateway settings\n"
        "EXISTING_TOKEN=<placeholder>\n"
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n"
        "HAIKU_MODEL=claude-haiku-4-5\n"
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
    assert "ANTHROPIC_API_KEY=<anthropic-api-key>" in rendered
    assert "HAIKU_MODEL=claude-haiku-4-5" in rendered
    assert "PERPLEXITY_API_KEY=<perplexity-api-key>" in rendered


def test_sync_service_env_files_appends_missing_keys_without_overwriting_values(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "GATEWAY_HOST=0.0.0.0\n"
        "GATEWAY_PUBLIC_HOST=<gateway.user.example.com>\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-token>\n"
        "GATEWAY_LOCAL_API_TOKEN=<local-token>\n"
        "WHATSAPP_BRIDGE_TOKEN=<bridge-token>\n"
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n"
        "HAIKU_MODEL=claude-haiku-4-5\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n",
        encoding="utf-8",
    )
    (backend_root / "gateway.env").write_text(
        "GATEWAY_HOST=0.0.0.0\n"
        "GATEWAY_PUBLIC_HOST=ec2-3-137-194-119.us-east-2.compute.amazonaws.com\n"
        "GATEWAY_INTERNAL_TOKEN=shared-token\n"
        "GATEWAY_LOCAL_API_TOKEN=local-real\n"
        "WHATSAPP_BRIDGE_TOKEN=bridge-real\n"
        "ANTHROPIC_API_KEY=anthropic-real\n"
        "HAIKU_MODEL=claude-haiku-4-5\n"
        "PERPLEXITY_API_KEY=perplexity-real\n",
        encoding="utf-8",
    )
    (backend_root / "model_router.env.example").write_text(
        "GROQ_API_KEY=<groq-api-key>\n"
        "CLASSIFIER_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )
    (backend_root / "orchestrator.env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-token>\n"
        "GATEWAY_SIGNING_SECRET=<signing-secret>\n"
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n",
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
    orchestrator_env_path = system_env_dir / "orchestrator.env"
    bridge_env_path = system_env_dir / "whatsapp-bridge.env"
    gateway_env_path.write_text(
        "GATEWAY_PUBLIC_HOST=<gateway.user.example.com>\n"
        "GATEWAY_INTERNAL_TOKEN=shared-token\n"
        "GATEWAY_LOCAL_API_TOKEN=local-real\n"
        "WHATSAPP_BRIDGE_TOKEN=bridge-real\n"
        "ANTHROPIC_API_KEY=anthropic-real\n"
        "EXISTING_KEY=keep-me\n",
        encoding="utf-8",
    )
    model_router_env_path.write_text(
        "GROQ_API_KEY=groq-real\n",
        encoding="utf-8",
    )
    orchestrator_env_path.write_text(
        "GATEWAY_INTERNAL_TOKEN=shared-token\n",
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
        if args[:2] == ["cat", str(orchestrator_env_path)]:
            return SimpleNamespace(stdout=orchestrator_env_path.read_text(encoding="utf-8"), returncode=0)
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
    orchestrator_rendered = orchestrator_env_path.read_text(encoding="utf-8")
    bridge_rendered = bridge_env_path.read_text(encoding="utf-8")

    assert gateway_rendered.count("GATEWAY_LOCAL_API_TOKEN=") == 1
    assert "GATEWAY_LOCAL_API_TOKEN=local-real" in gateway_rendered
    assert "GATEWAY_PUBLIC_HOST=ec2-3-137-194-119.us-east-2.compute.amazonaws.com" in gateway_rendered
    assert "ANTHROPIC_API_KEY=anthropic-real" in gateway_rendered
    assert "HAIKU_MODEL=claude-haiku-4-5" in gateway_rendered
    assert "PERPLEXITY_API_KEY=perplexity-real" in gateway_rendered
    assert "GATEWAY_SIGNING_SECRET=" in gateway_rendered
    assert "EXISTING_KEY=keep-me" in gateway_rendered

    assert model_router_rendered.count("GROQ_API_KEY=") == 1
    assert "GROQ_API_KEY=groq-real" in model_router_rendered
    assert "CLASSIFIER_MODEL=openai/gpt-oss-20b" in model_router_rendered

    assert orchestrator_rendered.count("GATEWAY_INTERNAL_TOKEN=") == 1
    assert "GATEWAY_INTERNAL_TOKEN=shared-token" in orchestrator_rendered
    assert "GATEWAY_SIGNING_SECRET=" in orchestrator_rendered
    assert "ANTHROPIC_API_KEY=anthropic-real" in orchestrator_rendered

    assert bridge_rendered.count("WHATSAPP_BRIDGE_TOKEN=") == 1
    assert "WHATSAPP_BRIDGE_TOKEN=bridge-real" in bridge_rendered
    assert "WHATSAPP_AUTH_DIR=/var/lib/cosmic/whatsapp/auth" in bridge_rendered


def test_normalize_bootstrap_env_payload_maps_current_supabase_shape() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://3.137.194.119:8080",
                "vm_dns": "ec2-3-137-194-119.us-east-2.compute.amazonaws.com",
            },
            "gateway_env": {
                "GATEWAY_LOCAL_API_TOKEN": "pg_live_token",
                "ANTHROPIC_API_KEY": "anthropic-live",
                "PERPLEXITY_API_KEY": "perplexity-live",
                "HAIKU_MODEL": "claude-haiku-4-5",
            },
            "orchestrator_env": {
                "ANTHROPIC_API_KEY": "anthropic-live",
                "OPUS_MODEL": "claude-opus-4-6",
            },
            "meeting_env": {
                "GROQ_API_KEY": "groq-live",
                "DEEPGRAM_API_KEY": "deepgram-live",
            },
        }
    )

    assert normalized["gateway.env"]["GATEWAY_LOCAL_API_TOKEN"] == "pg_live_token"
    assert normalized["gateway.env"]["GATEWAY_PUBLIC_HOST"] == "ec2-3-137-194-119.us-east-2.compute.amazonaws.com"
    assert normalized["model-router.env"]["GROQ_API_KEY"] == "groq-live"
    assert normalized["orchestrator.env"]["ANTHROPIC_MODEL"] == "claude-opus-4-6"


def test_materialize_bootstrap_env_files_updates_repo_envs(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "GATEWAY_PUBLIC_HOST=<gateway.user.example.com>\n"
        "GATEWAY_LOCAL_API_TOKEN=<desktop-local-api-token>\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "GATEWAY_SIGNING_SECRET=<gateway-signing-secret>\n"
        "WHATSAPP_BRIDGE_TOKEN=<whatsapp-bridge-token>\n"
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n"
        "HAIKU_MODEL=claude-haiku-4-5\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n",
        encoding="utf-8",
    )
    (backend_root / "model_router.env.example").write_text(
        "GROQ_API_KEY=<groq-api-key>\n"
        "CLASSIFIER_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )
    (backend_root / "orchestrator.env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "GATEWAY_SIGNING_SECRET=<gateway-signing-secret>\n"
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n"
        "ANTHROPIC_MODEL=claude-opus-4-6\n",
        encoding="utf-8",
    )
    (bridge_dir / ".env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "WHATSAPP_BRIDGE_TOKEN=<whatsapp-bridge-token>\n"
        "WHATSAPP_AUTH_DIR=<auth-dir>\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(bootstrap, "DEFAULT_BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_SYSTEM_ENV_DIR", system_env_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_WHATSAPP_AUTH_DIR", PurePosixPath("/var/lib/cosmic/whatsapp/auth"))
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_env_payload",
        lambda **kwargs: {
            "gateway.env": {
                "GATEWAY_PUBLIC_HOST": "ec2-3-137-194-119.us-east-2.compute.amazonaws.com",
                "GATEWAY_LOCAL_API_TOKEN": "pg_live_token",
                "ANTHROPIC_API_KEY": "anthropic-live",
                "PERPLEXITY_API_KEY": "perplexity-live",
                "HAIKU_MODEL": "claude-haiku-4-5",
            },
            "model-router.env": {
                "GROQ_API_KEY": "groq-live",
            },
            "orchestrator.env": {
                "ANTHROPIC_API_KEY": "anthropic-live",
                "ANTHROPIC_MODEL": "claude-opus-4-6",
            },
        },
    )

    written = bootstrap.materialize_bootstrap_env_files(
        [backend_root, backend_root / "bridges"],
        system_env_dir,
        bootstrap_token="bs_live_token",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
    )

    gateway_env_path = backend_root / "gateway.env"
    model_router_env_path = backend_root / "model_router.env"
    orchestrator_env_path = backend_root / "orchestrator.env"
    bridge_env_path = bridge_dir / ".env"

    assert gateway_env_path in written
    assert model_router_env_path in written
    assert orchestrator_env_path in written
    assert bridge_env_path in written

    gateway_rendered = gateway_env_path.read_text(encoding="utf-8")
    model_router_rendered = model_router_env_path.read_text(encoding="utf-8")
    orchestrator_rendered = orchestrator_env_path.read_text(encoding="utf-8")
    bridge_rendered = bridge_env_path.read_text(encoding="utf-8")

    assert "GATEWAY_LOCAL_API_TOKEN=pg_live_token" in gateway_rendered
    assert "GATEWAY_PUBLIC_HOST=ec2-3-137-194-119.us-east-2.compute.amazonaws.com" in gateway_rendered
    assert "ANTHROPIC_API_KEY=anthropic-live" in gateway_rendered
    assert "PERPLEXITY_API_KEY=perplexity-live" in gateway_rendered
    assert "GROQ_API_KEY=groq-live" in model_router_rendered
    assert "ANTHROPIC_MODEL=claude-opus-4-6" in orchestrator_rendered
    assert "WHATSAPP_AUTH_DIR=/var/lib/cosmic/whatsapp/auth" in bridge_rendered


def test_materialize_bootstrap_env_files_can_render_memory_env(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "COSMIC_MEMORY_URL=\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n",
        encoding="utf-8",
    )
    (backend_root / "model_router.env.example").write_text(
        "GROQ_API_KEY=<groq-api-key>\n",
        encoding="utf-8",
    )
    (backend_root / "orchestrator.env.example").write_text(
        "ANTHROPIC_API_KEY=<anthropic-api-key>\n"
        "ANTHROPIC_MODEL=claude-opus-4-6\n",
        encoding="utf-8",
    )
    (backend_root / "memory.env.example").write_text(
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n"
        "XAI_API_KEY=\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "COSMIC_MEMORY_INTERNAL_TOKEN=<internal-service-token>\n"
        "COSMIC_MEMORY_DATA_DIR=/var/lib/cosmic/memory\n",
        encoding="utf-8",
    )
    (bridge_dir / ".env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "WHATSAPP_BRIDGE_TOKEN=<whatsapp-bridge-token>\n"
        "WHATSAPP_AUTH_DIR=<auth-dir>\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(bootstrap, "DEFAULT_BRIDGE_DIR", bridge_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_SYSTEM_ENV_DIR", system_env_dir)
    monkeypatch.setattr(bootstrap, "DEFAULT_WHATSAPP_AUTH_DIR", PurePosixPath("/var/lib/cosmic/whatsapp/auth"))
    monkeypatch.setattr(bootstrap, "DEFAULT_MEMORY_DATA_DIR", PurePosixPath("/var/lib/cosmic/memory"))
    monkeypatch.setattr(
        bootstrap,
        "fetch_bootstrap_env_payload",
        lambda **kwargs: {
            "gateway.env": {
                "GATEWAY_INTERNAL_TOKEN": "shared-token",
                "PERPLEXITY_API_KEY": "perplexity-live",
            },
            "model-router.env": {
                "GROQ_API_KEY": "groq-live",
            },
            "orchestrator.env": {
                "ANTHROPIC_API_KEY": "anthropic-live",
                "ANTHROPIC_MODEL": "claude-opus-4-6",
            },
        },
    )

    written = bootstrap.materialize_bootstrap_env_files(
        [backend_root, backend_root / "bridges"],
        system_env_dir,
        bootstrap_token="bs_live_token",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        include_memory=True,
    )

    memory_env_path = backend_root / "memory.env"
    gateway_env_path = backend_root / "gateway.env"

    assert memory_env_path in written
    assert "COSMIC_MEMORY_URL=http://127.0.0.1:8090" in gateway_env_path.read_text(encoding="utf-8")
    memory_rendered = memory_env_path.read_text(encoding="utf-8")
    assert "PERPLEXITY_API_KEY=perplexity-live" in memory_rendered
    assert "GATEWAY_INTERNAL_TOKEN=shared-token" in memory_rendered
    assert "COSMIC_MEMORY_INTERNAL_TOKEN=shared-token" in memory_rendered
    assert "COSMIC_MEMORY_DATA_DIR=/var/lib/cosmic/memory" in memory_rendered


def test_fetch_bootstrap_env_payload_retries_transient_urlerror(monkeypatch) -> None:
    attempts = {"count": 0}
    payload = {
        "success": True,
        "vm": {
            "gateway_url": "https://user.thelearnchain.com",
            "vm_dns": "user.thelearnchain.com",
        },
        "gateway_env": {
            "GATEWAY_LOCAL_API_TOKEN": "pg_live_token",
            "ANTHROPIC_API_KEY": "anthropic-live",
            "PERPLEXITY_API_KEY": "perplexity-live",
            "HAIKU_MODEL": "claude-haiku-4-5",
        },
        "orchestrator_env": {
            "ANTHROPIC_API_KEY": "anthropic-live",
            "OPUS_MODEL": "claude-opus-4-6",
        },
        "meeting_env": {
            "GROQ_API_KEY": "groq-live",
        },
    }

    class FakeResponse:
        def __init__(self, body: str) -> None:
            self.body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return self.body

    def fake_urlopen(request, timeout=30):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise URLError("temporary network issue")
        return FakeResponse(json.dumps(payload))

    monkeypatch.setattr(bootstrap, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    normalized = bootstrap.fetch_bootstrap_env_payload(
        bootstrap_token="bs_live_token",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
    )

    assert attempts["count"] == 2
    assert normalized["gateway.env"]["GATEWAY_LOCAL_API_TOKEN"] == "pg_live_token"
    assert normalized["gateway.env"]["GATEWAY_PUBLIC_HOST"] == "user.thelearnchain.com"
    assert normalized["model-router.env"]["GROQ_API_KEY"] == "groq-live"
    assert normalized["orchestrator.env"]["ANTHROPIC_MODEL"] == "claude-opus-4-6"


def test_fetch_bootstrap_env_payload_does_not_retry_http_400(monkeypatch) -> None:
    attempts = {"count": 0}

    def fake_urlopen(request, timeout=30):
        attempts["count"] += 1
        raise HTTPError(
            url=request.full_url,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"message":"invalid bootstrap token"}'),
        )

    monkeypatch.setattr(bootstrap, "urlopen", fake_urlopen)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    try:
        bootstrap.fetch_bootstrap_env_payload(
            bootstrap_token="bs_live_token",
            supabase_url="https://example.supabase.co",
            supabase_anon_key="anon",
        )
    except bootstrap.BootstrapError as exc:
        assert "HTTP 400" in str(exc)
    else:
        raise AssertionError("Expected BootstrapError for HTTP 400 response")

    assert attempts["count"] == 1


def test_run_with_retry_retries_subprocess_failure(monkeypatch) -> None:
    attempts = {"count": 0}

    def fake_run(command, **kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise subprocess.CalledProcessError(returncode=1, cmd=command)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    result = bootstrap.run_with_retry(["python", "--version"], attempts=3, initial_delay_sec=0.01)

    assert attempts["count"] == 3
    assert result.returncode == 0


def test_setup_local_redis_installs_and_restarts_service(monkeypatch) -> None:
    installed_packages: list[tuple[str, list[str]]] = []
    executed_commands: list[list[str]] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: installed_packages.append((manager, list(packages))),
    )
    monkeypatch.setattr(
        bootstrap,
        "run",
        lambda command, **kwargs: executed_commands.append(list(command)) or SimpleNamespace(returncode=0, stdout=""),
    )

    bootstrap.setup_local_redis("redis://127.0.0.1:6379/0")

    assert installed_packages == [("apt-get", ["redis-server"])]
    assert executed_commands == [
        ["systemctl", "enable", "redis-server"],
        ["systemctl", "restart", "redis-server"],
    ]


def test_setup_local_redis_skips_non_local_redis_urls(monkeypatch) -> None:
    install_calls: list[list[str]] = []
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: install_calls.append(list(packages)),
    )

    bootstrap.setup_local_redis("redis://10.0.0.5:6379/0")

    assert install_calls == []
