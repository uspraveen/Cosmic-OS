from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cosmic_mail_client import normalize_cosmic_mail_base_url
from .sqlite_client import connect_sync

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_email_integration (
    slot TEXT PRIMARY KEY,
    configured INTEGER NOT NULL DEFAULT 1,
    base_url TEXT NOT NULL,
    api_token TEXT NOT NULL,
    primary_mailbox_address TEXT,
    webhook_secret TEXT,
    webhook_signature_header TEXT,
    updated_at TEXT NOT NULL
);
"""


@dataclass(slots=True)
class AgentEmailIntegrationRecord:
    configured: bool
    base_url: str
    api_token: str
    primary_mailbox_address: str
    webhook_secret: str
    webhook_signature_header: str
    updated_at: str


class AgentEmailIntegrationStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect_sync(self.db_path) as conn:
            conn.executescript(_SCHEMA_SQL)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(agent_email_integration)").fetchall()}
            if "configured" not in columns:
                conn.execute(
                    "ALTER TABLE agent_email_integration ADD COLUMN configured INTEGER NOT NULL DEFAULT 1"
                )
            conn.commit()

    def get_primary(self) -> AgentEmailIntegrationRecord | None:
        self.initialize()
        with connect_sync(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT configured, base_url, api_token, primary_mailbox_address, webhook_secret, webhook_signature_header, updated_at
                FROM agent_email_integration
                WHERE slot = 'primary'
                """
            ).fetchone()
        if row is None:
            return None
        return AgentEmailIntegrationRecord(
            configured=bool(int(row[0] or 0)),
            base_url=str(row[1] or "").strip(),
            api_token=str(row[2] or "").strip(),
            primary_mailbox_address=str(row[3] or "").strip(),
            webhook_secret=str(row[4] or "").strip(),
            webhook_signature_header=str(row[5] or "").strip() or "X-Cosmic-Mail-Signature",
            updated_at=str(row[6] or "").strip(),
        )

    def save_primary(
        self,
        *,
        base_url: str,
        api_token: str,
        primary_mailbox_address: str | None = None,
        webhook_secret: str | None = None,
        webhook_signature_header: str | None = None,
        updated_at: str,
    ) -> AgentEmailIntegrationRecord:
        normalized_base_url = normalize_cosmic_mail_base_url(base_url)
        normalized_api_token = str(api_token or "").strip()
        if not normalized_base_url or not normalized_api_token:
            raise ValueError("base_url and api_token are required")
        normalized_mailbox = str(primary_mailbox_address or "").strip()
        normalized_secret = str(webhook_secret or "").strip()
        normalized_signature_header = str(webhook_signature_header or "").strip() or "X-Cosmic-Mail-Signature"
        self.initialize()
        with connect_sync(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_email_integration (
                    slot,
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
                ) VALUES (
                    'primary',
                    1,
                    :base_url,
                    :api_token,
                    :primary_mailbox_address,
                    :webhook_secret,
                    :webhook_signature_header,
                    :updated_at
                )
                ON CONFLICT(slot) DO UPDATE SET
                    configured = excluded.configured,
                    base_url = excluded.base_url,
                    api_token = excluded.api_token,
                    primary_mailbox_address = excluded.primary_mailbox_address,
                    webhook_secret = excluded.webhook_secret,
                    webhook_signature_header = excluded.webhook_signature_header,
                    updated_at = excluded.updated_at
                """,
                {
                    "base_url": normalized_base_url,
                    "api_token": normalized_api_token,
                    "primary_mailbox_address": normalized_mailbox,
                    "webhook_secret": normalized_secret,
                    "webhook_signature_header": normalized_signature_header,
                    "updated_at": updated_at,
                },
            )
            conn.commit()
        return AgentEmailIntegrationRecord(
            configured=True,
            base_url=normalized_base_url,
            api_token=normalized_api_token,
            primary_mailbox_address=normalized_mailbox,
            webhook_secret=normalized_secret,
            webhook_signature_header=normalized_signature_header,
            updated_at=updated_at,
        )

    def clear_primary(self) -> None:
        self.initialize()
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with connect_sync(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_email_integration (
                    slot,
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
                ) VALUES (
                    'primary',
                    0,
                    '',
                    '',
                    '',
                    '',
                    'X-Cosmic-Mail-Signature',
                    :updated_at
                )
                ON CONFLICT(slot) DO UPDATE SET
                    configured = 0,
                    base_url = '',
                    api_token = '',
                    primary_mailbox_address = '',
                    webhook_secret = '',
                    webhook_signature_header = 'X-Cosmic-Mail-Signature',
                    updated_at = excluded.updated_at
                """,
                {"updated_at": updated_at},
            )
            conn.commit()


def agent_email_integration_is_configured(record: AgentEmailIntegrationRecord | None) -> bool:
    if record is None:
        return False
    return bool(record.configured and str(record.base_url or "").strip() and str(record.api_token or "").strip())


def agent_email_integration_is_disabled(record: AgentEmailIntegrationRecord | None) -> bool:
    if record is None:
        return False
    return not bool(record.configured)


def agent_email_integration_to_dict(record: AgentEmailIntegrationRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "configured": bool(record.configured),
        "base_url": record.base_url,
        "api_token": record.api_token,
        "primary_mailbox_address": record.primary_mailbox_address or None,
        "webhook_secret": record.webhook_secret or None,
        "webhook_signature_header": record.webhook_signature_header or "X-Cosmic-Mail-Signature",
        "updated_at": record.updated_at,
    }
