from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
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
    trusted_senders_json TEXT NOT NULL DEFAULT '[]',
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
    trusted_senders: tuple[str, ...]
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
            if "trusted_senders_json" not in columns:
                conn.execute(
                    "ALTER TABLE agent_email_integration ADD COLUMN trusted_senders_json TEXT NOT NULL DEFAULT '[]'"
                )
            conn.commit()

    def get_primary(self) -> AgentEmailIntegrationRecord | None:
        self.initialize()
        with connect_sync(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    trusted_senders_json,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
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
            trusted_senders=self._parse_trusted_senders(row[4]),
            webhook_secret=str(row[5] or "").strip(),
            webhook_signature_header=str(row[6] or "").strip() or "X-Cosmic-Mail-Signature",
            updated_at=str(row[7] or "").strip(),
        )

    def save_primary(
        self,
        *,
        base_url: str,
        api_token: str,
        primary_mailbox_address: str | None = None,
        trusted_senders: list[str] | tuple[str, ...] | None = None,
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
        normalized_trusted_senders = self._normalize_trusted_senders(trusted_senders)
        self.initialize()
        existing = self.get_primary()
        if trusted_senders is None and existing is not None:
            normalized_trusted_senders = existing.trusted_senders
        with connect_sync(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_email_integration (
                    slot,
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    trusted_senders_json,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
                ) VALUES (
                    'primary',
                    1,
                    :base_url,
                    :api_token,
                    :primary_mailbox_address,
                    :trusted_senders_json,
                    :webhook_secret,
                    :webhook_signature_header,
                    :updated_at
                )
                ON CONFLICT(slot) DO UPDATE SET
                    configured = excluded.configured,
                    base_url = excluded.base_url,
                    api_token = excluded.api_token,
                    primary_mailbox_address = excluded.primary_mailbox_address,
                    trusted_senders_json = excluded.trusted_senders_json,
                    webhook_secret = excluded.webhook_secret,
                    webhook_signature_header = excluded.webhook_signature_header,
                    updated_at = excluded.updated_at
                """,
                {
                    "base_url": normalized_base_url,
                    "api_token": normalized_api_token,
                    "primary_mailbox_address": normalized_mailbox,
                    "trusted_senders_json": json.dumps(list(normalized_trusted_senders), ensure_ascii=True),
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
            trusted_senders=tuple(normalized_trusted_senders),
            webhook_secret=normalized_secret,
            webhook_signature_header=normalized_signature_header,
            updated_at=updated_at,
        )

    def clear_primary(self) -> None:
        self.initialize()
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        existing = self.get_primary()
        trusted_senders_json = json.dumps(
            list(existing.trusted_senders) if existing is not None else [],
            ensure_ascii=True,
        )
        with connect_sync(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_email_integration (
                    slot,
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    trusted_senders_json,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
                ) VALUES (
                    'primary',
                    0,
                    '',
                    '',
                    '',
                    :trusted_senders_json,
                    '',
                    'X-Cosmic-Mail-Signature',
                    :updated_at
                )
                ON CONFLICT(slot) DO UPDATE SET
                    configured = 0,
                    base_url = '',
                    api_token = '',
                    primary_mailbox_address = '',
                    trusted_senders_json = excluded.trusted_senders_json,
                    webhook_secret = '',
                    webhook_signature_header = 'X-Cosmic-Mail-Signature',
                    updated_at = excluded.updated_at
                """,
                {
                    "trusted_senders_json": trusted_senders_json,
                    "updated_at": updated_at,
                },
            )
            conn.commit()

    def save_trusted_senders(
        self,
        trusted_senders: list[str] | tuple[str, ...],
        *,
        updated_at: str,
    ) -> AgentEmailIntegrationRecord:
        self.initialize()
        existing = self.get_primary()
        normalized_trusted_senders = self._normalize_trusted_senders(trusted_senders)
        configured = 1 if existing is None else int(bool(existing.configured))
        base_url = "" if existing is None else existing.base_url
        api_token = "" if existing is None else existing.api_token
        primary_mailbox_address = "" if existing is None else existing.primary_mailbox_address
        webhook_secret = "" if existing is None else existing.webhook_secret
        webhook_signature_header = (
            "X-Cosmic-Mail-Signature"
            if existing is None
            else existing.webhook_signature_header or "X-Cosmic-Mail-Signature"
        )
        with connect_sync(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO agent_email_integration (
                    slot,
                    configured,
                    base_url,
                    api_token,
                    primary_mailbox_address,
                    trusted_senders_json,
                    webhook_secret,
                    webhook_signature_header,
                    updated_at
                ) VALUES (
                    'primary',
                    :configured,
                    :base_url,
                    :api_token,
                    :primary_mailbox_address,
                    :trusted_senders_json,
                    :webhook_secret,
                    :webhook_signature_header,
                    :updated_at
                )
                ON CONFLICT(slot) DO UPDATE SET
                    trusted_senders_json = excluded.trusted_senders_json,
                    updated_at = excluded.updated_at
                """,
                {
                    "configured": configured,
                    "base_url": base_url,
                    "api_token": api_token,
                    "primary_mailbox_address": primary_mailbox_address,
                    "trusted_senders_json": json.dumps(list(normalized_trusted_senders), ensure_ascii=True),
                    "webhook_secret": webhook_secret,
                    "webhook_signature_header": webhook_signature_header,
                    "updated_at": updated_at,
                },
            )
            conn.commit()
        return AgentEmailIntegrationRecord(
            configured=bool(configured),
            base_url=base_url,
            api_token=api_token,
            primary_mailbox_address=primary_mailbox_address,
            trusted_senders=tuple(normalized_trusted_senders),
            webhook_secret=webhook_secret,
            webhook_signature_header=webhook_signature_header,
            updated_at=updated_at,
        )

    @staticmethod
    def _parse_trusted_senders(raw: Any) -> tuple[str, ...]:
        try:
            payload = json.loads(str(raw or "[]"))
        except Exception:
            payload = []
        return AgentEmailIntegrationStore._normalize_trusted_senders(payload)

    @staticmethod
    def _normalize_trusted_senders(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            candidates: list[Any] = [value]
        elif isinstance(value, (list, tuple)):
            candidates = list(value)
        else:
            candidates = []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            email = str(item or "").strip().lower()
            if not email or "@" not in email or "." not in email or any(ch.isspace() for ch in email):
                continue
            if email in seen:
                continue
            seen.add(email)
            normalized.append(email)
        return tuple(normalized)


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
        "trusted_senders": list(record.trusted_senders),
        "webhook_secret": record.webhook_secret or None,
        "webhook_signature_header": record.webhook_signature_header or "X-Cosmic-Mail-Signature",
        "updated_at": record.updated_at,
    }
