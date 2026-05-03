from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from gateway.agent_auth_store import AgentAuthStore


_LOCAL_TMP_ROOT = Path(r"C:\Users\Praveen Raj U S\.codex\memories\agent-auth-store-tests")
_LOCAL_TMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def _store_root():
    root = _LOCAL_TMP_ROOT / f"agent-auth-store-{uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        rmtree(root, ignore_errors=True)


def test_codex_auth_store_encrypts_api_key_and_returns_redacted_status() -> None:
    with _store_root() as root:
        db_path = root / "credentials.db"
        store = AgentAuthStore(db_path)
        store.initialize()

        saved = store.save_codex(
            auth_mode="api_key",
            preferred_model="gpt-5.3-codex",
            approval_mode="auto_edit",
            vm_sync_enabled=True,
            api_key="sk-test-secret-value",
            status="authenticated",
            last_cli_status={"ok": True},
        )

        assert saved["auth_mode"] == "api_key"
        assert saved["preferred_model"] == "gpt-5.3-codex"
        assert saved["approval_mode"] == "auto_edit"
        assert saved["has_api_key"] is True
        assert "api_key" not in saved

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT encrypted_api_key, has_api_key FROM agent_provider_auth WHERE provider = 'codex'"
            ).fetchone()
        assert row is not None
        assert row[0]
        assert row[0] != "sk-test-secret-value"
        assert row[1] == 1

        secret = store.get_codex(include_secret=True)
        assert secret["api_key"] == "sk-test-secret-value"


def test_codex_auth_store_logout_clears_secret_but_preserves_preferences() -> None:
    with _store_root() as root:
        store = AgentAuthStore(root / "credentials.db")
        store.save_codex(
            auth_mode="api_key",
            preferred_model="gpt-5.4",
            approval_mode="full_auto",
            vm_sync_enabled=False,
            api_key="sk-test-secret-value",
            status="authenticated",
        )

        cleared = store.clear_codex_api_key(
            status="logged_out",
            login_required_reason="user_logged_out",
            last_cli_status={"ok": True},
        )

        assert cleared["auth_mode"] == "api_key"
        assert cleared["preferred_model"] == "gpt-5.4"
        assert cleared["approval_mode"] == "full_auto"
        assert cleared["vm_sync_enabled"] is False
        assert cleared["has_api_key"] is False
        assert cleared["status"] == "logged_out"
        assert cleared["login_required_reason"] == "user_logged_out"
        assert store.get_codex(include_secret=True)["api_key"] == ""
