from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from shutil import rmtree
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest

import bootstrap


@pytest.fixture
def integration_root(tmp_path_factory: pytest.TempPathFactory):
    """Isolate `agent_email_integrations.db` so this test never touches the real DB."""
    root = tmp_path_factory.mktemp("bootstrap-cosmic-mail")
    db_path = root / "agent_email_integrations.db"
    yield db_path
    rmtree(root, ignore_errors=True)


def test_provision_cosmic_mail_returns_payload_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["data"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = dict(req.headers)
        body = json.dumps(
            {
                "success": True,
                "base_url": "https://console.thelearnchain.com",
                "organization": {"id": "org_1", "name": "Acme", "slug": "acme", "cosmic_user_id": "u1"},
                "api_key": {"id": "k1", "plaintext": "cm_org_test_xyz", "name": "k"},
                "mailbox": {"id": "mb1", "address": "cosmic_acme@mail.thelearnchain.com", "domain": "mail.thelearnchain.com"},
                "agent": {"id": "a1", "slug": "cosmic", "name": "Cosmic"},
            }
        ).encode("utf-8")
        return FakeResponse(body)

    monkeypatch.setattr(bootstrap, "urlopen", fake_urlopen)

    result = bootstrap.provision_cosmic_mail_org_via_edge_function(
        vm_api_token="pg_test_token_long_enough_value",
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-test-key",
    )

    assert result["success"] is True
    assert result["api_key"]["plaintext"] == "cm_org_test_xyz"
    assert captured["url"].endswith("/functions/v1/provision-cosmic-mail-org")
    assert captured["data"] == {"vm_api_token": "pg_test_token_long_enough_value"}
    assert captured["headers"]["Authorization"] == "Bearer anon-test-key"


def test_provision_cosmic_mail_raises_on_function_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return json.dumps({"success": False, "error": "vm_not_found"}).encode("utf-8")

    monkeypatch.setattr(bootstrap, "urlopen", lambda req, timeout: FakeResponse())

    with pytest.raises(bootstrap.BootstrapError, match="vm_not_found"):
        bootstrap.provision_cosmic_mail_org_via_edge_function(
            vm_api_token="pg_test_token_long_enough_value",
            supabase_url="https://example.supabase.co",
            supabase_anon_key="anon-test-key",
        )


def test_provision_cosmic_mail_wraps_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(req, timeout):
        raise HTTPError(req.full_url, 500, "boom", hdrs={}, fp=BytesIO(b"server fail"))

    monkeypatch.setattr(bootstrap, "urlopen", boom)
    monkeypatch.setattr(bootstrap, "should_retry_bootstrap_http_error", lambda exc: False)

    with pytest.raises(bootstrap.BootstrapError, match="HTTP 500"):
        bootstrap.provision_cosmic_mail_org_via_edge_function(
            vm_api_token="pg_test_token_long_enough_value",
            supabase_url="https://example.supabase.co",
            supabase_anon_key="anon-test-key",
        )


def test_persist_cosmic_mail_provisioning_writes_integration_store(
    monkeypatch: pytest.MonkeyPatch, integration_root: Path
) -> None:
    monkeypatch.setattr(bootstrap, "agent_email_integrations_db_path", lambda: integration_root)

    payload = {
        "success": True,
        "base_url": "https://console.thelearnchain.com",
        "api_key": {"id": "k1", "plaintext": "cm_org_zzzz"},
        "mailbox": {"id": "mb1", "address": "cosmic_someone@mail.thelearnchain.com"},
    }

    bootstrap.persist_cosmic_mail_provisioning(payload)

    state, env = bootstrap.read_agent_email_integration_state()
    assert state == "configured"
    assert env["COSMIC_MAIL_BASE_URL"] == "https://console.thelearnchain.com"
    assert env["COSMIC_MAIL_API_TOKEN"] == "cm_org_zzzz"
    assert env["COSMIC_MAIL_PRIMARY_MAILBOX_ADDRESS"] == "cosmic_someone@mail.thelearnchain.com"


def test_persist_cosmic_mail_provisioning_rejects_missing_fields(integration_root: Path) -> None:
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.persist_cosmic_mail_provisioning({"success": True})
