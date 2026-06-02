"""Tests for Gateway credential store and manager."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure backend root is importable
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from gateway.credentials.store import CredentialStore
from gateway.credentials.encryption import encrypt_token_str, decrypt_token


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    try:
        os.unlink(db_path)
    except PermissionError:
        pass  # Windows file locking — cleaned up eventually


@pytest.fixture
def store(tmp_db):
    """Create a CredentialStore backed by a temp database."""
    s = CredentialStore(tmp_db)
    yield s
    s.close()


# ── Account CRUD tests ───────────────────────────────────────────────────────


class TestAccountCRUD:
    def test_create_account(self, store):
        account = store.create_account(
            provider="google",
            provider_account_id="g_123",
            email="test@example.com",
            display_name="Test User",
        )
        assert account["account_id"].startswith("acc_")
        assert account["provider"] == "google"
        assert account["email"] == "test@example.com"
        assert account["status"] == "active"

    def test_get_account(self, store):
        created = store.create_account(provider="google", email="a@b.com")
        fetched = store.get_account(created["account_id"])
        assert fetched is not None
        assert fetched["email"] == "a@b.com"

    def test_get_nonexistent_account(self, store):
        assert store.get_account("acc_nonexistent") is None

    def test_list_accounts(self, store):
        store.create_account(provider="google", email="a@b.com")
        store.create_account(provider="google", email="c@d.com")
        accounts = store.list_accounts("google")
        assert len(accounts) == 2

    def test_list_accounts_filtered_by_provider(self, store):
        store.create_account(provider="google", email="a@b.com")
        store.create_account(provider="github", email="e@f.com")
        assert len(store.list_accounts("google")) == 1
        assert len(store.list_accounts("github")) == 1

    def test_update_account(self, store):
        account = store.create_account(provider="google", email="old@b.com")
        updated = store.update_account(
            account["account_id"],
            email="new@b.com",
            display_name="New Name",
        )
        assert updated["email"] == "new@b.com"
        assert updated["display_name"] == "New Name"

    def test_update_account_metadata_patch(self, store):
        account = store.create_account(
            provider="google",
            metadata={"avatar_url": "old.png"},
        )
        updated = store.update_account(
            account["account_id"],
            metadata_patch={"avatar_url": "new.png", "hosted_domain": "example.com"},
        )
        meta = updated.get("_metadata", {})
        assert meta.get("avatar_url") == "new.png"
        assert meta.get("hosted_domain") == "example.com"

    def test_delete_account(self, store):
        account = store.create_account(provider="google")
        store.delete_account(account["account_id"])
        assert store.get_account(account["account_id"]) is None

    def test_set_primary(self, store):
        a1 = store.create_account(provider="google", email="a@b.com")
        a2 = store.create_account(provider="google", email="c@d.com")
        store.set_primary(a2["account_id"])
        fetched = store.get_account(a2["account_id"])
        assert fetched["is_primary"] is True
        other = store.get_account(a1["account_id"])
        assert other["is_primary"] is False

    def test_get_account_by_provider_account(self, store):
        store.create_account(
            provider="google", provider_account_id="g_999", email="x@y.com"
        )
        found = store.get_account_by_provider_account("google", "g_999")
        assert found is not None
        assert found["email"] == "x@y.com"


# ── Credential CRUD tests ────────────────────────────────────────────────────


class TestCredentialCRUD:
    def test_store_credential(self, store):
        account = store.create_account(provider="google")
        ref = store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar", "email"],
            access_token="ya29.access_token",
            refresh_token="1//refresh_token",
            expires_at_ts=1700000000.0,
        )
        assert ref.startswith("cred_")

    def test_get_active_credential(self, store):
        account = store.create_account(provider="google")
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="token123",
            refresh_token="refresh123",
        )
        cred = store.get_active_credential(account["account_id"])
        assert cred is not None
        assert cred["access_token"] == "token123"
        assert cred["refresh_token"] == "refresh123"
        assert cred["granted_scopes"] == ["calendar"]

    def test_credential_encryption_roundtrip(self):
        """Verify tokens are encrypted and can be decrypted."""
        plaintext = "ya29.test_access_token"
        encrypted = encrypt_token_str(plaintext)
        assert encrypted != plaintext
        decrypted = decrypt_token(encrypted)
        assert decrypted == plaintext

    def test_revoke_account_credentials(self, store):
        account = store.create_account(provider="google")
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="tok",
            refresh_token="ref",
        )
        store.revoke_account_credentials(account["account_id"])
        cred = store.get_active_credential(account["account_id"])
        assert cred is None

    def test_update_access_token(self, store):
        account = store.create_account(provider="google")
        ref = store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="old_token",
            refresh_token="refresh",
        )
        store.update_access_token(ref, "new_token", 1800000000.0)
        cred = store.get_credential_by_ref(ref)
        assert cred["access_token"] == "new_token"
        assert cred["access_token_expires_at"] == 1800000000.0

    def test_new_credential_revokes_old(self, store):
        """Storing a new credential should revoke previous ones for the same account."""
        account = store.create_account(provider="google")
        ref1 = store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="tok1",
            refresh_token="ref1",
        )
        ref2 = store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="tok2",
            refresh_token="ref2",
        )
        old_cred = store.get_credential_by_ref(ref1)
        assert old_cred["revoked_at"] is not None
        new_cred = store.get_active_credential(account["account_id"])
        assert new_cred["credential_ref"] == ref2


# ── Audit tests ───────────────────────────────────────────────────────────────


class TestAudit:
    def test_log_audit(self, store):
        # Should not raise — audit is append-only, no FK constraints
        store.log_audit(
            action="resolve",
            provider="google",
            result="success",
            credential_ref="cred_test",
            scopes_used=["calendar"],
        )

    def test_revoke_account_cascades(self, store):
        account = store.create_account(provider="google")
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token="tok",
            refresh_token="ref",
        )
        store.delete_account(account["account_id"])
        assert store.get_account(account["account_id"]) is None
        assert store.get_active_credential(account["account_id"]) is None


@pytest.mark.asyncio
async def test_manager_resolve_credential_returns_account_metadata(store):
    from gateway.credentials.manager import CredentialManager

    account = store.create_account(
        provider="google",
        email="usp.upenn@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
        is_primary=True,
    )
    store.store_credential(
        account_id=account["account_id"],
        granted_scopes=["calendar"],
        access_token="tok_live",
        refresh_token="ref_live",
        expires_at_ts=time.time() + 3600,
    )
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        allow_primary_fallback=True,
        operation_mode="read",
    )

    assert resolved is not None
    assert resolved["account_id"] == account["account_id"]
    assert resolved["account_email"] == "usp.upenn@gmail.com"
    assert resolved["account_display_name"] == "Praveen Raj U S"
    assert resolved["account_label"] == "Google account"
    assert resolved["account_is_primary"] is True


@pytest.mark.asyncio
async def test_manager_resolves_exact_email_when_generic_labels_collide(store):
    from gateway.credentials.manager import CredentialManager

    first = store.create_account(
        provider="google",
        email="uspraveenraj@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
        is_primary=True,
    )
    second = store.create_account(
        provider="google",
        email="usp.upenn@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
    )
    for account in (first, second):
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token=f"tok_{account['account_id']}",
            refresh_token=f"ref_{account['account_id']}",
            expires_at_ts=time.time() + 3600,
        )
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        account_hint="uspraveenraj@gmail.com",
        operation_mode="write",
    )

    assert resolved is not None
    assert resolved["account_id"] == first["account_id"]
    assert resolved["account_email"] == "uspraveenraj@gmail.com"


@pytest.mark.asyncio
async def test_manager_resolves_email_inside_natural_account_hint(store):
    from gateway.credentials.manager import CredentialManager

    first = store.create_account(
        provider="google",
        email="uspraveenraj@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
        is_primary=True,
    )
    second = store.create_account(
        provider="google",
        email="usp.upenn@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
    )
    for account in (first, second):
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=[
                "https://www.googleapis.com/auth/documents",
                "https://www.googleapis.com/auth/drive",
            ],
            access_token=f"tok_{account['account_id']}",
            refresh_token=f"ref_{account['account_id']}",
            expires_at_ts=time.time() + 3600,
        )
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=[
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive",
        ],
        account_hint="use uspraveenraj@gmail.com for this Google Doc",
        operation_mode="write",
    )

    assert resolved is not None
    assert resolved["account_id"] == first["account_id"]
    assert resolved["account_email"] == "uspraveenraj@gmail.com"


@pytest.mark.asyncio
async def test_manager_resolves_explicit_primary_hint_for_write(store):
    from gateway.credentials.manager import CredentialManager

    first = store.create_account(
        provider="google",
        email="uspraveenraj@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
        is_primary=True,
    )
    second = store.create_account(
        provider="google",
        email="usp.upenn@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
    )
    for account in (first, second):
        store.store_credential(
            account_id=account["account_id"],
            granted_scopes=["calendar"],
            access_token=f"tok_{account['account_id']}",
            refresh_token=f"ref_{account['account_id']}",
            expires_at_ts=time.time() + 3600,
        )
    mgr = CredentialManager(store)

    resolved = await mgr.resolve_credential(
        provider="google",
        required_scopes=["calendar"],
        account_hint="primary",
        operation_mode="write",
    )

    assert resolved is not None
    assert resolved["account_id"] == first["account_id"]


def test_manager_exposes_email_display_label_for_generic_google_labels(store):
    from gateway.credentials.manager import CredentialManager

    account = store.create_account(
        provider="google",
        email="uspraveenraj@gmail.com",
        display_name="Praveen Raj U S",
        account_label="Google account",
    )
    store.store_credential(
        account_id=account["account_id"],
        granted_scopes=["calendar"],
        access_token="tok_live",
        refresh_token="ref_live",
        expires_at_ts=time.time() + 3600,
    )
    mgr = CredentialManager(store)

    listed = mgr.list_accounts("google")

    assert listed[0]["account_label"] == "Google account"
    assert listed[0]["account_display_label"] == "uspraveenraj@gmail.com"


# ── Resource binding tests ────────────────────────────────────────────────────


class TestResourceBindings:
    def test_upsert_and_lookup(self, store):
        account = store.create_account(provider="google")
        store.upsert_resource_binding(
            resource_type="google_doc",
            external_id="doc_123",
            account_id=account["account_id"],
            display_name="Project Proposal",
        )
        results = store.lookup_resource_binding(
            resource_type="google_doc", external_id="doc_123"
        )
        assert len(results) == 1
        assert results[0]["display_name"] == "Project Proposal"

    def test_lookup_by_display_name(self, store):
        account = store.create_account(provider="google")
        store.upsert_resource_binding(
            resource_type="calendar_event",
            external_id="cal_work",
            account_id=account["account_id"],
            display_name="Work Calendar",
        )
        results = store.lookup_resource_binding(
            resource_type="calendar_event", display_name="Work"
        )
        assert len(results) == 1
