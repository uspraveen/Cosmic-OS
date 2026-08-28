from __future__ import annotations

import io
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import bootstrap
from shared import AgentEmailIntegrationStore


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


def test_alpha_agent_env_defaults_cursor_to_grok_4_5_high(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    alpha_dir = backend_root / "agents" / "alpha_agent"
    system_env_dir = tmp_path / "etc" / "cosmic"
    alpha_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)
    (alpha_dir / "agent.env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=\n"
        "ORCHESTRATOR_INTERNAL_TOKEN=\n"
        "AGENT_SECRET=\n"
        "ALPHA_CURSOR_MODEL=\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    _dest, rendered, parsed = bootstrap.build_alpha_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="shared-token",
        system_env_dir=system_env_dir,
    )

    assert "ALPHA_CURSOR_MODEL=cursor-grok-4.5-high" in rendered
    assert parsed["ALPHA_CURSOR_MODEL"] == "cursor-grok-4.5-high"


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


def test_build_service_env_overrides_emits_fireworks_glm_defaults(tmp_path) -> None:
    gateway_source = tmp_path / "gateway.env"
    model_router_source = tmp_path / "model-router.env"
    orchestrator_source = tmp_path / "orchestrator.env"
    bridge_source = tmp_path / "whatsapp-bridge.env"
    gateway_source.write_text(
        "GATEWAY_INTERNAL_TOKEN=shared-token\n"
        "GATEWAY_SIGNING_SECRET=signing-token\n"
        "ANTHROPIC_API_KEY=anthropic-live\n"
        "FIREWORKS_API_KEY=fw-live\n",
        encoding="utf-8",
    )
    model_router_source.write_text("GROQ_API_KEY=groq-live\n", encoding="utf-8")
    orchestrator_source.write_text(
        "ANTHROPIC_MODEL=claude-opus-4-6\n",
        encoding="utf-8",
    )
    bridge_source.write_text("WHATSAPP_BRIDGE_TOKEN=bridge-token\n", encoding="utf-8")

    overrides = bootstrap.build_service_env_overrides(
        [
            (gateway_source, Path("/etc/cosmic/gateway.env")),
            (model_router_source, Path("/etc/cosmic/model-router.env")),
            (orchestrator_source, Path("/etc/cosmic/orchestrator.env")),
            (bridge_source, Path("/etc/cosmic/whatsapp-bridge.env")),
        ]
    )

    orchestrator_env = overrides["orchestrator.env"]
    assert orchestrator_env["COSMIC_ORCHESTRATOR_DEFAULT_PROVIDER"] == "fireworks_glm"
    assert orchestrator_env["ORCHESTRATOR_FIREWORKS_API_KEY"] == "fw-live"
    assert orchestrator_env["ORCHESTRATOR_FIREWORKS_KIMI_MODEL"] == "accounts/fireworks/models/kimi-k2p6"
    assert orchestrator_env["ORCHESTRATOR_FIREWORKS_GLM_MODEL"] == "accounts/fireworks/models/glm-5p2"
    assert orchestrator_env["ORCHESTRATOR_FIREWORKS_VISION_FALLBACK_MODEL"] == "accounts/fireworks/models/kimi-k2p6"


def test_normalize_bootstrap_env_payload_maps_vm_user_id_to_gateway_env() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://127.0.0.1:8080",
                "vm_dns": "localhost",
                "user_id": "user_supabase_123",
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
    )

    assert normalized["gateway.env"]["COSMIC_USER_ID"] == "user_supabase_123"


def test_normalize_bootstrap_env_payload_accepts_firecrawl_agent_env() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://127.0.0.1:8080",
                "vm_dns": "localhost",
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
            "firecrawl_agent_env": {
                "FIRECRAWL_API_KEY": "fc-live",
            },
        }
    )

    assert normalized[bootstrap.FIRECRAWL_AGENT_ENV_NAME]["FIRECRAWL_API_KEY"] == "fc-live"


def test_normalize_bootstrap_env_payload_accepts_email_agent_env() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://127.0.0.1:8080",
                "vm_dns": "localhost",
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
            "email_agent_env": {
                "COSMIC_MAIL_BASE_URL": "https://mail.example.com",
                "COSMIC_MAIL_API_TOKEN": "mail-token",
            },
        }
    )

    assert normalized[bootstrap.EMAIL_AGENT_ENV_NAME]["COSMIC_MAIL_BASE_URL"] == "https://mail.example.com"
    assert normalized[bootstrap.EMAIL_AGENT_ENV_NAME]["COSMIC_MAIL_API_TOKEN"] == "mail-token"


def test_normalize_bootstrap_env_payload_accepts_image_generator_agent_env() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://127.0.0.1:8080",
                "vm_dns": "localhost",
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
            "image_generator_agent_env": {
                "IMAGE_AGENT_XAI_API_KEY": "xai-live",
                "IMAGE_AGENT_OPENAI_API_KEY": "openai-live",
            },
        }
    )

    assert normalized[bootstrap.IMAGE_GENERATOR_AGENT_ENV_NAME]["IMAGE_AGENT_XAI_API_KEY"] == "xai-live"
    assert normalized[bootstrap.IMAGE_GENERATOR_AGENT_ENV_NAME]["IMAGE_AGENT_OPENAI_API_KEY"] == "openai-live"


def test_normalize_bootstrap_env_payload_accepts_memory_env() -> None:
    normalized = bootstrap.normalize_bootstrap_env_payload(
        {
            "success": True,
            "vm": {
                "gateway_url": "http://127.0.0.1:8080",
                "vm_dns": "localhost",
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
            "memory_env": {
                "XAI_API_KEY": "xai-live",
            },
        }
    )

    assert normalized["memory.env"]["XAI_API_KEY"] == "xai-live"


def test_materialize_bootstrap_env_files_updates_repo_envs(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    firecrawl_dir = backend_root / "agents" / "firecrawl_web_scrape"
    email_dir = backend_root / "agents" / "email_agent"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    firecrawl_dir.mkdir(parents=True)
    email_dir.mkdir(parents=True)
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
    (firecrawl_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=firecrawl-web-scrape-agent-1\n"
        "FIRECRAWL_API_KEY=<firecrawl-api-key>\n",
        encoding="utf-8",
    )
    (email_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=email-agent-1\n"
        "COSMIC_MAIL_BASE_URL=\n"
        "COSMIC_MAIL_API_TOKEN=\n"
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS=\n"
        "EMAIL_AGENT_INTERNAL_LLM_API_KEY=\n"
        "EMAIL_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "EMAIL_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
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
            "memory.env": {
                "XAI_API_KEY": "xai-live",
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


def test_install_service_env_files_installs_firecrawl_agent_env(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    firecrawl_dir = backend_root / "agents" / "firecrawl_web_scrape"
    email_dir = backend_root / "agents" / "email_agent"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    firecrawl_dir.mkdir(parents=True)
    email_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "GATEWAY_SIGNING_SECRET=<gateway-signing-secret>\n",
        encoding="utf-8",
    )
    (backend_root / "model_router.env").write_text(
        "GROQ_API_KEY=groq-real\n",
        encoding="utf-8",
    )
    (backend_root / "orchestrator.env.example").write_text(
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
    (firecrawl_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=firecrawl-web-scrape-agent-1\n"
        "FIRECRAWL_API_KEY=<firecrawl-api-key>\n",
        encoding="utf-8",
    )
    (email_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=email-agent-1\n"
        "COSMIC_MAIL_BASE_URL=\n"
        "COSMIC_MAIL_API_TOKEN=\n"
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS=\n"
        "EMAIL_AGENT_INTERNAL_LLM_API_KEY=\n"
        "EMAIL_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "EMAIL_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
        encoding="utf-8",
    )

    def fake_run(command, **kwargs):
        args = list(command)
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

    installed = bootstrap.install_service_env_files(system_env_dir)

    firecrawl_env_path = system_env_dir / "agents" / bootstrap.FIRECRAWL_AGENT_ENV_NAME
    assert firecrawl_env_path in installed
    firecrawl_rendered = firecrawl_env_path.read_text(encoding="utf-8")
    assert "GATEWAY_INTERNAL_TOKEN=<internal-service-token>" not in firecrawl_rendered
    assert "AGENT_SECRET=<agent-shared-secret>" not in firecrawl_rendered
    assert "FIRECRAWL_API_KEY=<firecrawl-api-key>" in firecrawl_rendered


def test_build_email_agent_env_rendered_prefers_external_values(tmp_path, monkeypatch) -> None:
    backend_root = tmp_path / "Backend"
    email_dir = backend_root / "agents" / "email_agent"
    email_dir.mkdir(parents=True)
    (email_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=email-agent-1\n"
        "COSMIC_MAIL_BASE_URL=\n"
        "COSMIC_MAIL_API_TOKEN=\n"
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS=\n"
        "EMAIL_AGENT_INTERNAL_LLM_API_KEY=\n"
        "EMAIL_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "EMAIL_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    dest_path, rendered, parsed = bootstrap.build_email_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="internal-token",
        external_env_by_name={
            bootstrap.EMAIL_AGENT_ENV_NAME: {
                "COSMIC_MAIL_BASE_URL": "https://mail.example.com",
                "COSMIC_MAIL_API_TOKEN": "mail-token",
                "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS": "assistant@example.com",
                "EMAIL_AGENT_INTERNAL_LLM_API_KEY": "internal_llm-key",
                "EMAIL_AGENT_INTERNAL_LLM_BASE_URL": "https://internal_llm.example.com/v1",
            }
        },
    )

    assert dest_path.name == bootstrap.EMAIL_AGENT_ENV_NAME
    assert "COSMIC_MAIL_BASE_URL=https://mail.example.com" in rendered
    assert parsed["COSMIC_MAIL_API_TOKEN"] == "mail-token"
    assert parsed["COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"] == "assistant@example.com"
    assert parsed["EMAIL_AGENT_INTERNAL_LLM_API_KEY"] == "internal_llm-key"
    assert parsed["AGENT_SECRET"] == "signing-secret"
    assert parsed["GATEWAY_INTERNAL_TOKEN"] == "internal-token"


def test_build_email_agent_env_rendered_respects_explicit_disconnect(tmp_path, monkeypatch) -> None:
    backend_root = tmp_path / "Backend"
    email_dir = backend_root / "agents" / "email_agent"
    email_dir.mkdir(parents=True)
    (email_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=email-agent-1\n"
        "COSMIC_MAIL_BASE_URL=\n"
        "COSMIC_MAIL_API_TOKEN=\n"
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS=\n"
        "EMAIL_AGENT_INTERNAL_LLM_API_KEY=\n"
        "EMAIL_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "EMAIL_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    store = AgentEmailIntegrationStore(backend_root / "gateway" / "agent_email_integrations.db")
    store.clear_primary()

    _, rendered, parsed = bootstrap.build_email_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="internal-token",
        existing_env_by_name={
            bootstrap.EMAIL_AGENT_ENV_NAME: {
                "COSMIC_MAIL_BASE_URL": "https://stale-mail.example.com",
                "COSMIC_MAIL_API_TOKEN": "stale-token",
                "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS": "stale@example.com",
            }
        },
    )

    assert "COSMIC_MAIL_BASE_URL=https://stale-mail.example.com" not in rendered
    assert parsed["COSMIC_MAIL_BASE_URL"] == ""
    assert parsed["COSMIC_MAIL_API_TOKEN"] == ""
    assert parsed["COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"] == ""


def test_build_image_generator_agent_env_rendered_prefers_external_values(tmp_path, monkeypatch) -> None:
    backend_root = tmp_path / "Backend"
    image_dir = backend_root / "agents" / "image_generator_agent"
    image_dir.mkdir(parents=True)
    (image_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=image-generator-agent-1\n"
        "IMAGE_AGENT_ROUTER_API_KEY=\n"
        "IMAGE_AGENT_ROUTER_BASE_URL=https://api.openai.com/v1\n"
        "IMAGE_AGENT_ROUTER_MODEL=gpt-5-mini\n"
        "IMAGE_AGENT_OPENAI_API_KEY=\n"
        "IMAGE_AGENT_OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "IMAGE_AGENT_OPENAI_MODEL=gpt-image-1.5\n"
        "IMAGE_AGENT_XAI_API_KEY=\n"
        "IMAGE_AGENT_XAI_BASE_URL=https://api.x.ai/v1\n"
        "IMAGE_AGENT_XAI_MODEL=grok-imagine-image-pro\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    dest_path, rendered, parsed = bootstrap.build_image_generator_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="internal-token",
        external_env_by_name={
            bootstrap.IMAGE_GENERATOR_AGENT_ENV_NAME: {
                "IMAGE_AGENT_XAI_API_KEY": "xai-key",
                "IMAGE_AGENT_OPENAI_API_KEY": "openai-key",
                "IMAGE_AGENT_ROUTER_MODEL": "gpt-5-mini",
            }
        },
    )

    assert dest_path.name == bootstrap.IMAGE_GENERATOR_AGENT_ENV_NAME
    assert "IMAGE_AGENT_XAI_API_KEY=xai-key" in rendered
    assert parsed["IMAGE_AGENT_XAI_API_KEY"] == "xai-key"
    assert parsed["IMAGE_AGENT_OPENAI_API_KEY"] == "openai-key"
    assert parsed["IMAGE_AGENT_ROUTER_MODEL"] == "gpt-5-mini"
    assert parsed["AGENT_SECRET"] == "signing-secret"
    assert parsed["GATEWAY_INTERNAL_TOKEN"] == "internal-token"


def test_build_slide_agent_env_rendered_inherits_visual_keys_from_peer_agents(
    tmp_path, monkeypatch
) -> None:
    backend_root = tmp_path / "Backend"
    slide_dir = backend_root / "agents" / "slide_agent"
    system_env_dir = tmp_path / "etc" / "cosmic"
    agents_env_dir = system_env_dir / "agents"
    slide_dir.mkdir(parents=True)
    agents_env_dir.mkdir(parents=True)
    (slide_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=slide-agent-1\n"
        "SLIDE_AGENT_FIREWORKS_API_KEY=\n"
        "PEXELS_API_KEY=\n"
        "FIRECRAWL_API_KEY=\n"
        "FIRECRAWL_API_BASE_URL=https://api.firecrawl.dev\n"
        "XAI_API_KEY=\n"
        "XAI_BASE_URL=https://api.x.ai/v1\n"
        "XAI_MODEL=grok-imagine-image-pro\n"
        "IMAGE_AGENT_DEFAULT_SIZE=1536x1024\n",
        encoding="utf-8",
    )
    (agents_env_dir / bootstrap.FIRECRAWL_AGENT_ENV_NAME).write_text(
        "FIRECRAWL_API_KEY=fc-key\n"
        "FIRECRAWL_API_BASE_URL=https://api.firecrawl.dev\n"
        "FIRECRAWL_REQUEST_TIMEOUT_SEC=120\n",
        encoding="utf-8",
    )
    (agents_env_dir / bootstrap.IMAGE_GENERATOR_AGENT_ENV_NAME).write_text(
        "IMAGE_AGENT_XAI_API_KEY=xai-key\n"
        "IMAGE_AGENT_XAI_BASE_URL=https://api.x.ai/v1\n"
        "IMAGE_AGENT_XAI_MODEL=grok-imagine-image-pro\n"
        "IMAGE_AGENT_DEFAULT_SIZE=1536x1024\n"
        "IMAGE_AGENT_DEFAULT_QUALITY=high\n"
        "IMAGE_AGENT_MAX_IMAGES_PER_REQUEST=4\n"
        "IMAGE_AGENT_MAX_PROMPT_CHARS=6000\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    dest_path, rendered, parsed = bootstrap.build_slide_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="internal-token",
        system_env_dir=system_env_dir,
        external_env_by_name={
            bootstrap.SLIDE_AGENT_ENV_NAME: {
                "SLIDE_AGENT_FIREWORKS_API_KEY": "internal_llm-key",
                "PEXELS_API_KEY": "pexels-key",
            }
        },
    )

    assert dest_path.name == bootstrap.SLIDE_AGENT_ENV_NAME
    assert "PEXELS_API_KEY=pexels-key" in rendered
    assert "FIRECRAWL_API_KEY=fc-key" in rendered
    assert "XAI_API_KEY=xai-key" in rendered
    assert parsed["SLIDE_AGENT_FIREWORKS_API_KEY"] == "internal_llm-key"
    assert parsed["PEXELS_API_KEY"] == "pexels-key"
    assert parsed["FIRECRAWL_API_KEY"] == "fc-key"
    assert parsed["XAI_API_KEY"] == "xai-key"
    assert parsed["IMAGE_AGENT_DEFAULT_SIZE"] == "1536x1024"
    assert parsed["IMAGE_AGENT_DEFAULT_QUALITY"] == "high"
    assert parsed["AGENT_SECRET"] == "signing-secret"
    assert parsed["GATEWAY_INTERNAL_TOKEN"] == "internal-token"


def test_build_map_agent_env_rendered_inherits_openai_key_from_peer_agents(
    tmp_path, monkeypatch
) -> None:
    backend_root = tmp_path / "Backend"
    map_dir = backend_root / "agents" / "map_agent"
    map_dir.mkdir(parents=True)
    (map_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=map-agent-1\n"
        "MAP_AGENT_INTERNAL_LLM_API_KEY=\n"
        "MAP_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "MAP_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    dest_path, rendered, parsed = bootstrap.build_map_agent_env_rendered(
        signing_secret="signing-secret",
        shared_internal_token="internal-token",
        existing_env_by_name={
            bootstrap.DOCS_PARSER_AGENT_ENV_NAME: {"OPENAI_API_KEY": "openai-key"}
        },
    )

    assert dest_path.name == bootstrap.MAP_AGENT_ENV_NAME
    assert "MAP_AGENT_INTERNAL_LLM_API_KEY=openai-key" in rendered
    assert parsed["MAP_AGENT_INTERNAL_LLM_API_KEY"] == "openai-key"
    assert parsed["MAP_AGENT_INTERNAL_LLM_BASE_URL"] == "https://api.openai.com/v1"
    assert parsed["AGENT_SECRET"] == "signing-secret"
    assert parsed["GATEWAY_INTERNAL_TOKEN"] == "internal-token"


def test_build_visual_enhancement_env_rendered_inherits_shared_keys_from_peer_envs(
    tmp_path, monkeypatch
) -> None:
    backend_root = tmp_path / "Backend"
    system_env_dir = tmp_path / "etc" / "cosmic"
    agents_env_dir = system_env_dir / "agents"
    backend_root.mkdir(parents=True)
    agents_env_dir.mkdir(parents=True)
    (backend_root / "visual_enhancement.env.example").write_text(
        "VISUAL_ENHANCEMENT_ENABLED=true\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_API_KEY=\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL=https://api.fireworks.ai/inference/v1\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_MODEL=accounts/fireworks/models/kimi-k2p6\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_VISION_MODEL=accounts/fireworks/models/kimi-k2p6\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_REASONING_EFFORT=low\n"
        "VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC=20\n"
        "VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY=\n"
        "VISUAL_ENHANCEMENT_FIRECRAWL_BASE_URL=https://api.firecrawl.dev\n"
        "VISUAL_ENHANCEMENT_FIRECRAWL_REQUEST_TIMEOUT_SEC=20\n"
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED=true\n"
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL=https://www.bing.com/images/search\n"
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC=12\n"
        "VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT=8\n",
        encoding="utf-8",
    )
    (agents_env_dir / bootstrap.FIRECRAWL_AGENT_ENV_NAME).write_text(
        "FIRECRAWL_API_KEY=fc-key\n"
        "FIRECRAWL_API_BASE_URL=https://api.firecrawl.dev\n"
        "FIRECRAWL_REQUEST_TIMEOUT_SEC=120\n",
        encoding="utf-8",
    )
    (agents_env_dir / bootstrap.SLIDE_AGENT_ENV_NAME).write_text(
        "MODEL_API_KEY=fw-slide-key\n"
        "MODEL_BASE_URL=https://api.fireworks.ai/inference/v1\n"
        "MODEL_NAME=accounts/fireworks/models/kimi-k2p6\n"
        "MODEL_TIMEOUT_SEC=90\n",
        encoding="utf-8",
    )
    (system_env_dir / "orchestrator.env").write_text(
        "ANTHROPIC_API_KEY=anthropic-key\n"
        "MODEL_API_KEY=fw-orch-key\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bootstrap, "BACKEND_ROOT", backend_root)

    dest_path, rendered, parsed = bootstrap.build_visual_enhancement_env_rendered(
        system_env_dir=system_env_dir
    )

    assert dest_path.name == "visual_enhancement.env"
    assert "VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY=fc-key" in rendered
    assert parsed["VISUAL_ENHANCEMENT_FIRECRAWL_API_KEY"] == "fc-key"
    assert parsed["VISUAL_ENHANCEMENT_FIREWORKS_API_KEY"] == "fw-orch-key"
    assert (
        parsed["VISUAL_ENHANCEMENT_FIREWORKS_BASE_URL"]
        == "https://api.fireworks.ai/inference/v1"
    )
    assert (
        parsed["VISUAL_ENHANCEMENT_FIREWORKS_MODEL"]
        == "accounts/fireworks/models/kimi-k2p6"
    )
    assert parsed["VISUAL_ENHANCEMENT_FIREWORKS_TIMEOUT_SEC"] == "90"
    assert parsed["VISUAL_ENHANCEMENT_IMAGE_SEARCH_ENABLED"] == "true"
    assert (
        parsed["VISUAL_ENHANCEMENT_IMAGE_SEARCH_BASE_URL"]
        == "https://www.bing.com/images/search"
    )
    assert parsed["VISUAL_ENHANCEMENT_IMAGE_SEARCH_TIMEOUT_SEC"] == "12"
    assert parsed["VISUAL_ENHANCEMENT_IMAGE_SEARCH_RESULT_LIMIT"] == "8"
    assert parsed["VISUAL_ENHANCEMENT_IMAGE_CONTACT_SHEET_ENABLED"] == "true"
    assert parsed["VISUAL_ENHANCEMENT_IMAGE_CONTACT_SHEET_LIMIT"] == "10"
    assert (
        parsed["VISUAL_ENHANCEMENT_IMAGE_CONTACT_SHEET_CANDIDATE_MAX_BYTES"]
        == "2097152"
    )


def test_materialize_bootstrap_env_files_can_render_memory_env(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    firecrawl_dir = backend_root / "agents" / "firecrawl_web_scrape"
    email_dir = backend_root / "agents" / "email_agent"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    firecrawl_dir.mkdir(parents=True)
    email_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "COSMIC_MEMORY_URL=\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n"
        "XAI_API_KEY=\n"
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
    (firecrawl_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=firecrawl-web-scrape-agent-1\n"
        "FIRECRAWL_API_KEY=<firecrawl-api-key>\n",
        encoding="utf-8",
    )
    (email_dir / "agent.env.example").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n"
        "GATEWAY_URL=http://127.0.0.1:8080\n"
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "AGENT_SECRET=<agent-shared-secret>\n"
        "INSTANCE_ID=email-agent-1\n"
        "COSMIC_MAIL_BASE_URL=\n"
        "COSMIC_MAIL_API_TOKEN=\n"
        "COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS=\n"
        "EMAIL_AGENT_INTERNAL_LLM_API_KEY=\n"
        "EMAIL_AGENT_INTERNAL_LLM_BASE_URL=\n"
        "EMAIL_AGENT_INTERNAL_LLM_MODEL=gpt-5-mini\n",
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
            "memory.env": {
                "XAI_API_KEY": "xai-live",
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
    gateway_rendered = gateway_env_path.read_text(encoding="utf-8")
    assert "COSMIC_MEMORY_URL=http://127.0.0.1:8090" in gateway_rendered
    assert "XAI_API_KEY=xai-live" in gateway_rendered
    memory_rendered = memory_env_path.read_text(encoding="utf-8")
    assert "PERPLEXITY_API_KEY=perplexity-live" in memory_rendered
    assert "XAI_API_KEY=xai-live" in memory_rendered
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


def test_build_service_env_overrides_generates_neo4j_defaults_and_password(monkeypatch, tmp_path) -> None:
    backend_root = tmp_path / "Backend"
    bridge_dir = backend_root / "bridges" / "whatsapp_bridge"
    system_env_dir = tmp_path / "etc" / "cosmic"
    bridge_dir.mkdir(parents=True)
    system_env_dir.mkdir(parents=True)

    (backend_root / "gateway.env.example").write_text(
        "GATEWAY_INTERNAL_TOKEN=<internal-service-token>\n"
        "PERPLEXITY_API_KEY=<perplexity-api-key>\n",
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
        "COSMIC_MEMORY_GRAPH_BACKEND=neo4j\n"
        "COSMIC_MEMORY_NEO4J_URI=\n"
        "COSMIC_MEMORY_NEO4J_USERNAME=\n"
        "COSMIC_MEMORY_NEO4J_PASSWORD=<neo4j-password>\n"
        "COSMIC_MEMORY_NEO4J_DATABASE=\n",
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
    monkeypatch.setattr(bootstrap, "DEFAULT_MEMORY_DATA_DIR", PurePosixPath("/var/lib/cosmic/memory"))
    monkeypatch.setattr(bootstrap, "DEFAULT_WHATSAPP_AUTH_DIR", PurePosixPath("/var/lib/cosmic/whatsapp/auth"))

    effective_sources = bootstrap.resolve_effective_service_env_sources(system_env_dir, include_memory=True)
    overrides = bootstrap.build_service_env_overrides(effective_sources, include_memory=True)

    memory_env = overrides["memory.env"]
    assert memory_env["COSMIC_MEMORY_GRAPH_BACKEND"] == "neo4j"
    assert memory_env["COSMIC_MEMORY_NEO4J_URI"] == bootstrap.DEFAULT_NEO4J_URI
    assert memory_env["COSMIC_MEMORY_NEO4J_USERNAME"] == bootstrap.DEFAULT_NEO4J_USERNAME
    assert memory_env["COSMIC_MEMORY_NEO4J_DATABASE"] == bootstrap.DEFAULT_NEO4J_DATABASE
    assert memory_env["COSMIC_MEMORY_NEO4J_PASSWORD"]
    assert "<neo4j-password>" not in memory_env["COSMIC_MEMORY_NEO4J_PASSWORD"]
    assert len(memory_env["COSMIC_MEMORY_NEO4J_PASSWORD"]) >= 24


def test_ensure_memory_repo_checkout_clones_missing_repo(monkeypatch, tmp_path) -> None:
    target_repo = tmp_path / "cosmic-memory"
    commands: list[list[str]] = []

    def fake_run_with_retry(command, **kwargs):
        commands.append(list(command))
        target_repo.mkdir(parents=True, exist_ok=True)
        (target_repo / "pyproject.toml").write_text("[project]\nname='cosmic-memory'\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bootstrap, "ensure_git_available", lambda: None)
    monkeypatch.setattr(bootstrap, "run_with_retry", fake_run_with_retry)

    resolved = bootstrap.ensure_memory_repo_checkout(
        target_repo,
        "https://github.com/uspraveen/cosmic-memory.git",
        "main",
    )

    assert resolved == target_repo.resolve()
    assert commands == [[
        "git",
        "clone",
        "--depth",
        "1",
        "--branch",
        "main",
        "https://github.com/uspraveen/cosmic-memory.git",
        str(target_repo.resolve()),
    ]]


def test_verify_critical_backend_dependencies_checks_required_imports(monkeypatch, tmp_path) -> None:
    venv_path = tmp_path / ".venv"
    python_path = venv_path / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(
        bootstrap,
        "run",
        lambda command, **kwargs: commands.append(list(command)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    bootstrap.verify_critical_backend_dependencies(venv_path)

    assert commands == [
        [
            str(python_path),
            "-c",
            "import importlib; importlib.import_module('{0}')".format(module_name),
        ]
        for module_name, _label in bootstrap.CRITICAL_VENV_IMPORT_CHECKS
    ]


def test_verify_critical_backend_dependencies_raises_clear_error(monkeypatch, tmp_path) -> None:
    venv_path = tmp_path / ".venv"
    python_path = venv_path / "bin" / "python"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        args = list(command)
        if "docling" in args[-1]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=args,
                output="",
                stderr="ModuleNotFoundError: No module named 'docling'",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bootstrap, "run", fake_run)

    try:
        bootstrap.verify_critical_backend_dependencies(venv_path)
    except bootstrap.BootstrapError as exc:
        assert "docling" in str(exc)
        assert "Critical dependency check failed" in str(exc)
    else:
        raise AssertionError("Expected BootstrapError when a critical dependency import fails")


def test_ensure_office_renderer_installs_and_verifies(monkeypatch) -> None:
    install_calls: list[tuple[str, list[str]]] = []
    versions = [None, "LibreOffice 24.2.7.2"]

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: install_calls.append((manager, list(packages))),
    )
    monkeypatch.setattr(bootstrap, "office_renderer_version", lambda: versions.pop(0))

    bootstrap.ensure_office_renderer()

    assert install_calls == [("apt-get", ["libreoffice"])]


def test_ensure_pdf_renderer_installs_and_verifies(monkeypatch) -> None:
    install_calls: list[tuple[str, list[str]]] = []
    versions = [None, "pdftoppm version 24.02.0"]

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: install_calls.append((manager, list(packages))),
    )
    monkeypatch.setattr(bootstrap, "pdf_renderer_version", lambda: versions.pop(0))

    bootstrap.ensure_pdf_renderer()

    assert install_calls == [("apt-get", ["poppler-utils"])]


def test_ensure_slide_python_build_dependencies_installs_cairo_deps(monkeypatch) -> None:
    install_calls: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: install_calls.append((manager, list(packages))),
    )

    bootstrap.ensure_slide_python_build_dependencies()

    assert install_calls == [("apt-get", ["pkg-config", "libcairo2-dev"])]


def test_setup_python_verifies_critical_backend_dependencies(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "ensure_office_renderer", lambda: calls.append("ensure_office_renderer"))
    monkeypatch.setattr(bootstrap, "ensure_pdf_renderer", lambda: calls.append("ensure_pdf_renderer"))
    monkeypatch.setattr(bootstrap, "ensure_slide_python_build_dependencies", lambda: calls.append("ensure_slide_python_build_dependencies"))
    monkeypatch.setattr(bootstrap, "ensure_python3_available", lambda: calls.append("ensure_python3_available"))
    monkeypatch.setattr(bootstrap, "ensure_pip", lambda: calls.append("ensure_pip"))
    monkeypatch.setattr(bootstrap, "ensure_venv_support", lambda: calls.append("ensure_venv_support"))
    monkeypatch.setattr(bootstrap, "ensure_virtualenv", lambda path: calls.append("ensure_virtualenv"))
    monkeypatch.setattr(bootstrap, "upgrade_venv_pip", lambda path: calls.append("upgrade_venv_pip"))
    monkeypatch.setattr(bootstrap, "install_python_requirements", lambda venv, reqs: calls.append("install_python_requirements"))
    monkeypatch.setattr(bootstrap, "verify_critical_backend_dependencies", lambda path: calls.append("verify_critical_backend_dependencies"))
    monkeypatch.setattr(bootstrap, "ensure_playwright_chromium", lambda path: calls.append("ensure_playwright_chromium"))

    bootstrap.setup_python(tmp_path / ".venv", tmp_path / "requirements.txt")

    assert calls == [
        "ensure_office_renderer",
        "ensure_pdf_renderer",
        "ensure_slide_python_build_dependencies",
        "ensure_python3_available",
        "ensure_pip",
        "ensure_venv_support",
        "ensure_virtualenv",
        "upgrade_venv_pip",
        "install_python_requirements",
        "verify_critical_backend_dependencies",
        "ensure_playwright_chromium",
    ]


def test_ensure_neo4j_apt_repository_writes_repo_and_key(monkeypatch, tmp_path) -> None:
    keyring_path = tmp_path / "etc" / "apt" / "keyrings" / "neotechnology.gpg"
    source_path = tmp_path / "etc" / "apt" / "sources.list.d" / "neo4j.list"
    installed_packages: list[tuple[str, list[str]]] = []
    retried_commands: list[list[str]] = []
    executed_commands: list[list[str]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return b"fake-key"

    @contextmanager
    def fake_tempdir(prefix: str = ""):
        temp_dir = tmp_path / "{0}temp".format(prefix or "neo4j-")
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            yield str(temp_dir)
        finally:
            pass

    def fake_run(command, **kwargs):
        args = list(command)
        executed_commands.append(args)
        if args[:3] == ["install", "-d", "-m"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(stdout="", returncode=0)
        if args[0] == "gpg":
            output_index = args.index("--output") + 1
            Path(args[output_index]).write_bytes(b"binary-key")
            return SimpleNamespace(stdout="", returncode=0)
        if args[0] == "install" and "-m" in args:
            src_path = Path(args[-2])
            dest_path = Path(args[-1])
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.exists():
                dest_path.write_bytes(src_path.read_bytes())
            return SimpleNamespace(stdout="", returncode=0)
        raise AssertionError("Unexpected command: {0}".format(args))

    monkeypatch.setattr(bootstrap, "detect_package_manager", lambda: "apt-get")
    monkeypatch.setattr(bootstrap, "is_ubuntu_host", lambda: True)
    monkeypatch.setattr(bootstrap, "apt_has_candidate", lambda _package: False)
    monkeypatch.setattr(
        bootstrap,
        "install_system_packages",
        lambda manager, packages: installed_packages.append((manager, list(packages))),
    )
    monkeypatch.setattr(bootstrap, "run_with_retry", lambda command, **kwargs: retried_commands.append(list(command)) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap, "urlopen", lambda request, timeout=30: FakeResponse())
    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", fake_tempdir)
    monkeypatch.setattr(bootstrap, "DEFAULT_NEO4J_APT_KEYRING_PATH", keyring_path)
    monkeypatch.setattr(bootstrap, "DEFAULT_NEO4J_APT_SOURCE_PATH", source_path)
    monkeypatch.setattr(
        bootstrap,
        "DEFAULT_NEO4J_APT_SOURCE",
        "deb [signed-by={0}] https://debian.neo4j.com stable latest".format(keyring_path),
    )

    bootstrap.ensure_neo4j_apt_repository()

    assert installed_packages == [
        ("apt-get", ["ca-certificates", "gpg"]),
        ("apt-get", ["software-properties-common"]),
    ]
    assert ["add-apt-repository", "-y", "universe"] in retried_commands
    assert ["apt-get", "update"] in retried_commands
    assert keyring_path.exists()
    assert source_path.read_text(encoding="utf-8").strip().endswith("https://debian.neo4j.com stable latest")


def test_setup_neo4j_rotates_default_password_when_needed(monkeypatch, tmp_path) -> None:
    memory_env_path = tmp_path / "memory.env"
    memory_env_path.write_text(
        "COSMIC_MEMORY_GRAPH_BACKEND=neo4j\n"
        "COSMIC_MEMORY_NEO4J_URI=bolt://127.0.0.1:7687\n"
        "COSMIC_MEMORY_NEO4J_USERNAME=neo4j\n"
        "COSMIC_MEMORY_NEO4J_PASSWORD=DesiredSecret1234567890\n",
        encoding="utf-8",
    )
    executed_commands: list[list[str]] = []
    initial_passwords: list[str] = []
    rotations: list[tuple[str, str, str, str]] = []
    auth_attempts = {"desired": 0, "default": 0}

    def fake_auth(uri, username, password):
        if password == "DesiredSecret1234567890":
            auth_attempts["desired"] += 1
            return auth_attempts["desired"] >= 2
        if password == "neo4j":
            auth_attempts["default"] += 1
            return True
        return False

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap, "ensure_neo4j_package_installed", lambda: True)
    monkeypatch.setattr(
        bootstrap,
        "sync_assignment_file",
        lambda path, **kwargs: executed_commands.append(["sync_assignment_file", str(path), kwargs["overrides"]["server.default_listen_address"]]),
    )
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(bootstrap, "read_text_file", lambda path, **kwargs: Path(path).read_text(encoding="utf-8"))
    monkeypatch.setattr(bootstrap, "run", lambda command, **kwargs: executed_commands.append(list(command)) or SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(bootstrap, "wait_for_tcp_endpoint", lambda uri, timeout_sec=45.0: None)
    monkeypatch.setattr(bootstrap, "neo4j_auth_works", fake_auth)
    monkeypatch.setattr(bootstrap, "set_neo4j_initial_password", lambda password: initial_passwords.append(password))
    monkeypatch.setattr(
        bootstrap,
        "rotate_neo4j_password",
        lambda uri, username, current_password, new_password: rotations.append(
            (uri, username, current_password, new_password)
        ),
    )

    bootstrap.setup_neo4j(memory_env_path)

    assert initial_passwords == ["DesiredSecret1234567890"]
    assert ["systemctl", "enable", "neo4j"] in executed_commands
    assert ["systemctl", "restart", "neo4j"] in executed_commands
    assert rotations == [("bolt://127.0.0.1:7687", "neo4j", "neo4j", "DesiredSecret1234567890")]


def test_wait_for_health_endpoint_polls_until_ready(monkeypatch) -> None:
    attempts = {"count": 0}

    def fake_fetch_json(url: str, *, timeout_sec: float = 5.0):
        attempts["count"] += 1
        if attempts["count"] < 3:
            return {"status": "starting"}
        return {"status": "ready"}

    monkeypatch.setattr(bootstrap, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)

    payload = bootstrap.wait_for_health_endpoint(
        "http://127.0.0.1:8080/health/ready",
        check_name="gateway",
        timeout_sec=0.5,
        poll_interval_sec=0.01,
    )

    assert payload["status"] == "ready"
    assert attempts["count"] == 3


def test_run_post_provision_health_checks_uses_core_service_order(monkeypatch) -> None:
    systemd_checks: list[str] = []
    health_checks: list[tuple[str, str]] = []
    agent_checks: list[str] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(
        bootstrap,
        "wait_for_systemd_unit_active",
        lambda unit_name, **kwargs: systemd_checks.append(unit_name),
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_health_endpoint",
        lambda url, *, check_name, **kwargs: health_checks.append((check_name, url)) or {"status": "ok"},
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_orchestrator_agent_ready",
        lambda agent_id, **kwargs: agent_checks.append(agent_id) or {"status": "ok"},
    )

    bootstrap.run_post_provision_health_checks(include_memory=True, timeout_sec=1.0, poll_interval_sec=0.01)

    assert systemd_checks == [
        "cosmic-model-router.service",
        "cosmic-orchestrator.service",
        "cosmic-gateway.service",
        "cosmic-docs-parser-agent.service",
        "cosmic-tabular-agent.service",
        bootstrap.CALENDAR_AGENT_SERVICE_NAME,
        bootstrap.DIAGRAM_AGENT_SERVICE_NAME,
        bootstrap.SLIDE_AGENT_SERVICE_NAME,
        "cosmic-whatsapp-bridge.service",
        "cosmic-memory.service",
        bootstrap.TABULAR_AGENT_SERVICE_NAME,
    ]
    assert agent_checks == [bootstrap.TABULAR_AGENT_ID]
    assert health_checks == [
        ("orchestrator", "http://127.0.0.1:8743/health"),
        ("memory", "http://127.0.0.1:8090/health"),
        ("gateway", "http://127.0.0.1:8080/health/ready"),
    ]


def test_run_post_provision_health_checks_waits_for_firecrawl_agent(monkeypatch) -> None:
    systemd_checks: list[str] = []
    health_checks: list[tuple[str, str]] = []
    agent_checks: list[str] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(
        bootstrap,
        "wait_for_systemd_unit_active",
        lambda unit_name, **kwargs: systemd_checks.append(unit_name),
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_health_endpoint",
        lambda url, *, check_name, **kwargs: health_checks.append((check_name, url)) or {"status": "ok"},
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_orchestrator_agent_ready",
        lambda agent_id, **kwargs: agent_checks.append(agent_id) or {"status": "ok"},
    )

    bootstrap.run_post_provision_health_checks(
        include_memory=False,
        include_firecrawl_agent=True,
        timeout_sec=1.0,
        poll_interval_sec=0.01,
    )

    assert systemd_checks == [
        "cosmic-model-router.service",
        "cosmic-orchestrator.service",
        "cosmic-gateway.service",
        "cosmic-docs-parser-agent.service",
        "cosmic-tabular-agent.service",
        bootstrap.CALENDAR_AGENT_SERVICE_NAME,
        bootstrap.DIAGRAM_AGENT_SERVICE_NAME,
        bootstrap.SLIDE_AGENT_SERVICE_NAME,
        "cosmic-whatsapp-bridge.service",
        bootstrap.FIRECRAWL_AGENT_SERVICE_NAME,
        bootstrap.TABULAR_AGENT_SERVICE_NAME,
    ]
    assert agent_checks == [bootstrap.FIRECRAWL_AGENT_ID, bootstrap.TABULAR_AGENT_ID]
    assert health_checks == [
        ("orchestrator", "http://127.0.0.1:8743/health"),
        ("gateway", "http://127.0.0.1:8080/health/ready"),
    ]


def test_run_post_provision_health_checks_waits_for_image_generator_agent(monkeypatch) -> None:
    systemd_checks: list[str] = []
    health_checks: list[tuple[str, str]] = []
    agent_checks: list[str] = []

    monkeypatch.setattr(bootstrap, "is_linux", lambda: True)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(
        bootstrap,
        "wait_for_systemd_unit_active",
        lambda unit_name, **kwargs: systemd_checks.append(unit_name),
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_health_endpoint",
        lambda url, *, check_name, **kwargs: health_checks.append((check_name, url)) or {"status": "ok"},
    )
    monkeypatch.setattr(
        bootstrap,
        "wait_for_orchestrator_agent_ready",
        lambda agent_id, **kwargs: agent_checks.append(agent_id) or {"status": "ok"},
    )

    bootstrap.run_post_provision_health_checks(
        include_memory=False,
        include_image_generator_agent=True,
        timeout_sec=1.0,
        poll_interval_sec=0.01,
    )

    assert bootstrap.IMAGE_GENERATOR_AGENT_SERVICE_NAME in systemd_checks
    assert bootstrap.IMAGE_GENERATOR_AGENT_ID in agent_checks
    assert health_checks == [
        ("orchestrator", "http://127.0.0.1:8743/health"),
        ("gateway", "http://127.0.0.1:8080/health/ready"),
    ]
