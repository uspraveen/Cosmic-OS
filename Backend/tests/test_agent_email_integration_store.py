from __future__ import annotations

import sqlite3
from pathlib import Path

from shared.agent_email_integration_store import (
    AgentEmailIntegrationStore,
    agent_email_integration_is_configured,
    agent_email_integration_is_disabled,
)


def test_agent_email_integration_store_clear_primary_marks_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_email_integrations.db"
    store = AgentEmailIntegrationStore(db_path)

    store.save_primary(
        base_url="https://mail.example.com",
        api_token="mail-token",
        primary_mailbox_address="assistant@example.com",
        updated_at="2026-03-26T00:00:00Z",
    )
    store.clear_primary()

    record = store.get_primary()
    assert record is not None
    assert record.configured is False
    assert record.base_url == ""
    assert record.api_token == ""
    assert record.trusted_senders == ()
    assert agent_email_integration_is_disabled(record) is True
    assert agent_email_integration_is_configured(record) is False


def test_agent_email_integration_store_clear_primary_preserves_trusted_senders(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_email_integrations.db"
    store = AgentEmailIntegrationStore(db_path)

    store.save_primary(
        base_url="https://mail.example.com",
        api_token="mail-token",
        primary_mailbox_address="assistant@example.com",
        trusted_senders=["Owner@Example.com"],
        updated_at="2026-03-26T00:00:00Z",
    )
    store.clear_primary()

    record = store.get_primary()
    assert record is not None
    assert record.configured is False
    assert record.trusted_senders == ("owner@example.com",)


def test_agent_email_integration_store_save_trusted_senders_without_connection(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_email_integrations.db"
    store = AgentEmailIntegrationStore(db_path)

    record = store.save_trusted_senders(
        ["Owner@Example.com", "owner@example.com", "ops@example.com"],
        updated_at="2026-03-27T00:00:00Z",
    )

    assert record.configured is True
    assert record.base_url == ""
    assert record.api_token == ""
    assert record.trusted_senders == ("owner@example.com", "ops@example.com")
    persisted = store.get_primary()
    assert persisted is not None
    assert persisted.trusted_senders == ("owner@example.com", "ops@example.com")
    assert agent_email_integration_is_disabled(persisted) is False
    assert agent_email_integration_is_configured(persisted) is False


def test_agent_email_integration_store_migrates_legacy_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_email_integrations.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE agent_email_integration (
                slot TEXT PRIMARY KEY,
                base_url TEXT NOT NULL,
                api_token TEXT NOT NULL,
                primary_mailbox_address TEXT,
                webhook_secret TEXT,
                webhook_signature_header TEXT,
                updated_at TEXT NOT NULL
            );
            INSERT INTO agent_email_integration (
                slot, base_url, api_token, primary_mailbox_address, webhook_secret, webhook_signature_header, updated_at
            ) VALUES (
                'primary',
                'https://mail.example.com',
                'mail-token',
                'assistant@example.com',
                '',
                'X-Cosmic-Mail-Signature',
                '2026-03-26T00:00:00Z'
            );
            """
        )
        conn.commit()

    store = AgentEmailIntegrationStore(db_path)
    store.initialize()
    record = store.get_primary()

    assert record is not None
    assert record.configured is True
    assert record.base_url == "https://mail.example.com"
    assert record.trusted_senders == ()
    assert agent_email_integration_is_configured(record) is True
